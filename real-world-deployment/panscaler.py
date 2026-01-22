import os
import time
import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import math
from collections import deque
from prometheus_query import *


class ServiceScaler:
    def __init__(self):
        # Config from environment variables
        self.service_name = os.getenv("SERVICE_NAME")
        self.entrypoint = os.getenv("ENTRYPOINT")
        if not self.service_name or not self.entrypoint:
            raise ValueError("SERVICE_NAME and ENTRYPOINT must be set")

        self.prometheus_url = os.getenv(
            "PROMETHEUS_URL",
            "http://prometheus.monitoring.svc.cluster.local:9090",
        )
        self.poll_interval = int(os.getenv("POLL_INTERVAL", 45))

        self.cooldown_period = math.ceil(int(os.getenv("SCALE_COOLDOWN", 60)) / self.poll_interval)
        self.margin = float(os.getenv("MARGIN", 1.3))
        self.cooldown = 0

        self.local_slo_s = float(os.getenv("LATENCY_THRESHOLD_MS", 500)) / 1000
        self.sys_slo_s = float(os.getenv("SYS_THRESHOLD_MS", 1500)) / 1000

        self.samples = int(os.getenv("HISTORY", 30))
        self.capacity_history = deque(maxlen=self.samples)
        self.fanout_history = deque(maxlen=self.samples)

        # Kubernetes client
        config.load_incluster_config()
        self.apps_api = client.AppsV1Api()
        self.namespace = os.getenv("NAMESPACE", "default")
        self.deployment_name = self.service_name

        # Previous metrics for knee detection
        self.prev_elements = {"polished_lat": 0, "cpu": 0, "rps": 0}
        self.EPS_LAT = 0.10  # 10% latency inflation
        self.EPS_CPU = 0.05  # 5% CPU growth ceiling
        self.probing_scale_down = False

    def is_at_knee(self, polished_lat, cpu_per_replica):
        prev = self.prev_elements
        if prev["polished_lat"] <= 0 or prev["cpu"] <= 0.01:
            return False, None, None
        r_T = polished_lat / prev["polished_lat"]
        r_CPU = cpu_per_replica / prev["cpu"]
        knee = (r_T >= 1.0 + self.EPS_LAT) and (1 <= r_CPU <= 1.0 + self.EPS_CPU)
        return knee, r_T, r_CPU

    def query_prometheus(self, query: str, aggregate="sum") -> float:
        url = f"{self.prometheus_url}/api/v1/query"
        try:
            resp = requests.get(url, params={"query": query}, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            values = [float(result["value"][1]) for result in data["data"]["result"]]
            if not values:
                return 0.0
            return sum(values) if aggregate == "sum" else sum(values) / len(values)
        except Exception as e:
            print(f"[{self.service_name}] Prometheus query failed: {e}")
            return 0.0

    def get_metrics(self):
        inbound_workload = self.query_prometheus(inbound_query)
        inbound_entrypoint = self.query_prometheus(inbound_entrypoint_query)
        total_latency_s = self.query_prometheus(duration_sum_query) / 1000
        latency_elements = self.query_prometheus(duration_count_query)
        total_cpu = self.query_prometheus(cpu_usage_query)
        successful_requests = self.query_prometheus(successful_requests_query)
        retried_requests = self.query_prometheus(retried_requests_query)
        cpu_period = self.query_prometheus(cpu_period_query)
        cpu_spec_total_quota = self.query_prometheus(cpu_spec_total_quota_query)
        cpu_spec_istio_quota = self.query_prometheus(cpu_spec_istio_quota_query)
        cpu_pod_quota = (cpu_spec_total_quota - cpu_spec_istio_quota) / cpu_period

        polished_total_lat, max_downstream = 0, 0
        if latency_elements != 0:
            avg_total_latency = total_latency_s / latency_elements
            max_downstream = self.query_prometheus(max_downstream_latency_query) / 1000
            polished_total_lat = max(avg_total_latency - max_downstream, 0.0)

        return (
            inbound_workload,
            inbound_entrypoint,
            polished_total_lat,
            max_downstream,
            successful_requests,
            retried_requests,
            total_cpu,
            cpu_pod_quota,
        )

    def run_loop(self):
        min_instances = self.get_current_replicas()
        instances = min_instances
        capacity, fanout = None, None

        while True:
            # --- Collect metrics ---
            (
                local_inbound_rps,
                entrypoint_rps,
                polished_total_lat,
                max_downstream,
                successful_requests,
                retried_requests,
                total_cpu,
                cpu_pod_quota,
            ) = self.get_metrics()

            cpu_per_replica = total_cpu / instances
            cpu_usage = cpu_per_replica / cpu_pod_quota

            # Compute rates
            prev_rps = self.prev_elements["rps"]
            rps_delta_pct = ((local_inbound_rps - prev_rps) / prev_rps) if prev_rps > 0 else 0
            successful_rate = (successful_requests / local_inbound_rps) if local_inbound_rps > 0 else 0
            retry_rate = (retried_requests / local_inbound_rps) if local_inbound_rps > 0 else 0

            # Determine if system is steady
            steady = (
                self.cooldown == 0
                and successful_rate > 0.999
                and retry_rate < 0.05
                and abs(rps_delta_pct) < 0.1
            )

            # --- Probe for knee if scale-down was triggered ---
            knee, r_T, r_CPU = False, None, None
            if self.probing_scale_down:
                if steady:
                    knee, r_T, r_CPU = self.is_at_knee(polished_total_lat, cpu_per_replica)
                    # Disarm probe if knee found or cannot scale down
                    if knee or instances == min_instances:
                        self.probing_scale_down = False
                else:
                    self.probing_scale_down = False

            # --- Learn capacity and fanout ---
            if polished_total_lat < self.local_slo_s and not knee:
                curr_capacity = local_inbound_rps / instances
                self.capacity_history.append(curr_capacity)
                capacity = self._compute_capacity()

            if steady:
                fanout_sample = local_inbound_rps / entrypoint_rps
                self.fanout_history.append(fanout_sample)
                fanout = self._compute_fanout()

            clear_overprovisioned = (
                instances > min_instances
                and cpu_usage < 0.5
                and max_downstream < self.sys_slo_s
                and steady
            )

            # --- Logging ---
            print(
                f"[{self.service_name}] "
                f"RPS_local={local_inbound_rps:.2f}, "
                f"RPS_entry={entrypoint_rps:.2f}, "
                f"C={capacity:.2f if capacity else 'N/A'}, "
                f"Fout={fanout:.2f if fanout else 'N/A'}, "
                f"E[S]={polished_total_lat:.2f}s, "
                f"MAX(E[D])={max_downstream:.2f}s, "
                f"CPU%={(cpu_usage*100):.2f}%, "
                f"r_T={r_T if r_T is not None else float('nan'):.2f}, "
                f"r_CPU={r_CPU if r_CPU is not None else float('nan'):.2f}, "
                f"KNEE={knee}, "
                f"INST={instances}"
            )

            self.cooldown = max(0, self.cooldown - 1)

            # --- Scaling decision ---
            if self.cooldown == 0 or abs(rps_delta_pct) >= 0.1 or knee:
                self.prev_elements["rps"] = local_inbound_rps

                if capacity is not None and capacity > 0:
                    desired_replicas = (
                        max(min_instances, math.ceil(local_inbound_rps / capacity))
                        if fanout is None
                        else max(min_instances, math.ceil(fanout * entrypoint_rps / capacity))
                    )

                    scale_up = desired_replicas > instances and polished_total_lat > self.local_slo_s
                    scale_down = desired_replicas < instances and clear_overprovisioned
                    scale_condition = scale_up or scale_down

                    if scale_condition:
                        print(f"[{self.service_name}] Scaling {instances} -> {desired_replicas} replicas")
                        self.scale(desired_replicas)

                        if desired_replicas > instances:
                            self.cooldown = self.cooldown_period
                        elif desired_replicas < instances:
                            # Update prev_elements for knee detection
                            self.prev_elements["polished_lat"] = polished_total_lat
                            self.prev_elements["cpu"] = cpu_per_replica
                            self.probing_scale_down = True

                        instances = self.get_current_replicas()

            time.sleep(self.poll_interval)

    def get_current_replicas(self) -> int:
        dep = self.apps_api.read_namespaced_deployment(
            name=self.deployment_name, namespace=self.namespace
        )
        return dep.status.ready_replicas or dep.spec.replicas or 0

    def scale(self, replicas: int):
        body = {"spec": {"replicas": replicas}}
        try:
            self.apps_api.patch_namespaced_deployment(
                name=self.deployment_name, namespace=self.namespace, body=body
            )
            print(f"[{self.service_name}] Scaled Deployment {self.deployment_name} to {replicas} replicas")
        except ApiException as e:
            print(f"[{self.service_name}] Failed to scale Deployment: {e}")

    def _compute_capacity(self):
        n = len(self.capacity_history)
        if n < 5:
            return sum(self.capacity_history) / n
        sorted_caps = sorted(self.capacity_history)
        trim = math.floor(0.25 * n)
        upper = sorted_caps[trim:]
        return sum(upper) / len(upper)

    def _compute_fanout(self):
        n = len(self.fanout_history)
        if n < 5:
            return sum(self.fanout_history) / n
        sorted_samples = sorted(self.fanout_history)
        trim = math.floor(0.25 * n)
        middle = sorted_samples[trim : n - trim]
        return sum(middle) / len(middle)


if __name__ == "__main__":
    scaler = ServiceScaler()
    scaler.run_loop()
