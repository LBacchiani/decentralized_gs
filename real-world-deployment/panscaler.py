import os
import time
import requests
from capacity_learner import ServiceCapacityLearner
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import math

class ServiceScaler:
    def __init__(self):
        # Config from environment variables
        self.service_name = os.getenv("SERVICE_NAME")
        if not self.service_name:
            raise ValueError("SERVICE_NAME must be set")
        self.entrypoint = os.getenv("ENTRYPOINT")
        if not self.entrypoint:
            raise ValueError("ENTRYPOINT must be set")

        self.prometheus_url = os.getenv(
            "PROMETHEUS_URL",
            "http://prometheus.monitoring.svc.cluster.local:9090",
        )
        self.poll_interval = int(os.getenv("POLL_INTERVAL", 45))

        self.cooldown_period = math.ceil(int((os.getenv("SCALE_COOLDOWN", 60))) /  self.poll_interval)  # seconds
        self.cooldown = 0
        

        # Initialize state
        self.slo_s = float(os.getenv("LATENCY_THRESHOLD_MS", 500)) / 1000
        self.learner = ServiceCapacityLearner(self.service_name, self.prometheus_url,  self.slo_s, self.poll_interval)
        
        # Kubernetes client
        config.load_incluster_config()
        self.apps_api = client.AppsV1Api()

        self.namespace = os.getenv("NAMESPACE", "default")
        self.deployment_name = self.service_name


    def query_prometheus(self, query: str) -> float:
        url = f"{self.prometheus_url}/api/v1/query"
        try:
            resp = requests.get(url, params={"query": query}, timeout=5)
            resp.raise_for_status()
            data = resp.json()

            values = [
                float(result["value"][1])
                for result in data["data"]["result"]
            ]

            if not values:
                return 0.0

            # average across series (replicas / pods)
            return sum(values) / len(values)

        except Exception as e:
            print(f"[{self.service_name}] Prometheus query failed: {e}")
            return 0.0

    def get_metrics(self):

        cpu_usage_query = f"""rate(container_cpu_usage_seconds_total{{pod=~"{self.service_name}.*", pod!~".*autoscaler.*", container="server"}}[{self.poll_interval}s]) * 100000"""
        cpu_limit_query = f"""container_spec_cpu_quota{{pod=~"{self.service_name}.*", pod!~".*autoscaler.*", container="server"}}"""
        mem_usage_query = f"""avg(container_memory_working_set_bytes{{pod=~"{self.service_name}.*", pod!~".*autoscaler.*", container="server"}})"""
        mem_limit_query = f"""avg(container_spec_memory_limit_bytes{{pod=~"{self.service_name}.*", pod!~".*autoscaler.*", container="server"}})"""
        # --- Inbound throughput (req/s) ---
        inbound_query = f"""
        sum(
          rate(istio_requests_total{{
            destination_workload="{self.service_name}",
            reporter="source"
          }}[{self.poll_interval}s])
        )
        """

        # --- Outbound latency to downstreams (p95, ms) ---
        downstream_latency_query = f"""
        max(
            histogram_quantile(
                0.95,
                sum(
                rate(istio_request_duration_milliseconds_bucket{{
                    source_workload="{self.service_name}",
                    reporter="destination"
                }}[{self.poll_interval}s])
                ) by (le, destination_service)
            )
        )
        """

        # --- Total latency for requests from this service (p95, ms) ---
        total_latency_query = f"""
        histogram_quantile(
          0.95,
          sum(
            rate(istio_request_duration_milliseconds_bucket{{
              destination_workload="{self.service_name}",
              reporter="source"
            }}[{self.poll_interval}s])
          ) by (le)
        )
        """
        # ---Total requests entering the system ---
        inbound_entrypoint_query = f"""
        sum(
            rate(istio_requests_total{{
                destination_workload="{self.entrypoint}",
                reporter="source"
            }}[{self.poll_interval}s])
        )
        """
        successful_requests_query = f"""
            sum(
                rate(istio_requests_total{{
                    destination_workload="{self.service_name}",
                    reporter="source",
                    response_code!~"5.."
                }}[{self.poll_interval}s])
            )
        """

        retried_requests = f"""
        sum(
            rate(istio_requests_total{{
                source_workload="{self.service_name}",
                reporter="destination",
                response_flags=~".*R.*"
            }}[{self.poll_interval}s])
        )
        """



        local_inbound_rps = self.query_prometheus(inbound_query)
        entrypoint_rps = self.query_prometheus(inbound_entrypoint_query)
        total_latency_s = self.query_prometheus(total_latency_query) / 1000.0
        downstream_latency_s = self.query_prometheus(downstream_latency_query) / 1000.0
        successful_requests = self.query_prometheus(successful_requests_query)
        retried_requests = self.query_prometheus(retried_requests)
        cpu_usage = self.query_prometheus(cpu_usage_query)
        cpu_limit = self.query_prometheus(cpu_limit_query)
        mem_usage = self.query_prometheus(mem_usage_query)
        mem_limit = self.query_prometheus(mem_limit_query)
        cpu_p = 0
        mem_p = 0 
        if cpu_limit > 0:
            cpu_p = cpu_usage/cpu_limit
        if mem_limit > 0:
            mem_p = mem_usage/mem_limit

        # Local tail latency = total - downstream
        local_latency_s = max(total_latency_s - downstream_latency_s, 0.0)
        successful_rate = successful_requests / local_inbound_rps
        retried_rate = retried_requests / local_inbound_rps
        return local_inbound_rps, entrypoint_rps, local_latency_s, downstream_latency_s, successful_rate, retried_rate cpu_p if cpu_p > 0 else None, mem_p if mem_p > 0  else None

    def run_loop(self):
        min_instances = self.get_current_replicas()
        instances = min_instances
        capacity = None
        update_inst = False
        while True:
            local_inbound_rps, local_latency_s, downstream_latency_s, cpu_p, mem_p = self.get_metrics()
            print(
                f"[{self.service_name}] REQ/s={local_inbound_rps:.2f}, "
                f"LocalLatency={local_latency_s:.2f}s, "
                f"DownstreamLatency={downstream_latency_s:.2f}s, "
                f"{f'capacity={capacity: .2f} ' if capacity else f'capacity=not available, '}"
                f"{f'CPU={cpu_p:.2f}%, ' if cpu_p else 'CPU=not available, '}"
                f"{f'MEM={mem_p:.2f}%, ' if mem_p else 'MEM=not available, '}"
                f"{f'INST={instances} '}"
                f"{f'MIN INST={min_instances}'}"
            )

            self.cooldown = max(0,self.cooldown - 1)
            if self.cooldown == 0:
                self.learner.update_metrics(request_rate=local_inbound_rps,local_latency_s=local_latency_s, cpu_p=cpu_p, mem_p=mem_p, current_replicas=instances, min_replicas=min_instances)
            # --- Scaling decision ---
                capacity = self.learner.get_capacity()
                saturated = local_latency_s > self.slo_s
                if capacity is not None and capacity > 0:
                    desired_replicas = max(min_instances, math.ceil(req_rate / capacity))
                    scale_condition = (desired_replicas > instances and saturated) or (desired_replicas < instances and not saturated)
                    if scale_condition:
                        print(f"[{self.service_name}] Scaling {instances} -> {desired_replicas} replicas")
                        self.scale(desired_replicas)
                        if desired_replicas > instances: 
                            self.cooldown = self.cooldown_period  
                            self.learner.underload_episode_reset()                          
                        update_inst = True
            time.sleep(self.poll_interval)
            if update_inst:
                instances = self.get_current_replicas()
                update_inst = False

    def get_current_replicas(self) -> int:
        dep = self.apps_api.read_namespaced_deployment(
            name=self.deployment_name,
            namespace=self.namespace,
        )
        return dep.status.ready_replicas or dep.spec.replicas or 0

    def scale(self, replicas: int):
        body = {
            "spec": {
                "replicas": replicas
            }
        }

        try:
            self.apps_api.patch_namespaced_deployment(
                name=self.deployment_name,
                namespace=self.namespace,
                body=body,
            )
            print(
                f"[{self.service_name}] Scaled Deployment "
                f"{self.deployment_name} to {replicas} replicas"
            )

        except ApiException as e:
            print(
                f"[{self.service_name}] Failed to scale Deployment: {e}"
            )



if __name__ == "__main__":
    scaler = ServiceScaler()
    scaler.run_loop()
