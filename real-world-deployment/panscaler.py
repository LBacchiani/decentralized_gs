import os
import time
import requests
from capacity_learner import ServiceCapacityLearner
from fanout_learner import ServiceFanoutLearner
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
        self.margin = float(os.getenv("MARGIN",1.3))
        self.cooldown = 0
        

        # Initialize state
        self.slo_s = float(os.getenv("LATENCY_THRESHOLD_MS", 500)) / 1000
        self.capacity_learner = ServiceCapacityLearner(self.service_name, self.slo_s)
        self.fanout_learner = ServiceFanoutLearner(self.service_name)

        # Kubernetes client
        config.load_incluster_config()
        self.apps_api = client.AppsV1Api()

        self.namespace = os.getenv("NAMESPACE", "default")
        self.deployment_name = self.service_name


    def query_prometheus(self, query: str, aggregate="sum") -> float:
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

            if aggregate == "sum":
                return sum(values)
            elif aggregate == "avg":
                return sum(values) / len(values)

        except Exception as e:
            print(f"[{self.service_name}] Prometheus query failed: {e}")
            return 0.0
        
    def get_load_metrics(self):
        inbound_query = f"""
            sum(
            rate(istio_requests_total{{
                destination_workload="{self.service_name}",
                reporter="destination"
            }}[{self.poll_interval}s])
            )
        """
        inbound_entrypoint_query = f"""
            sum(
                rate(istio_requests_total{{
                    destination_workload="{self.entrypoint}",
                    reporter="destination"
                }}[{self.poll_interval}s])
            )
        """

        inbound_workload = self.query_prometheus(inbound_query)
        inbound_entrypoint = self.query_prometheus(inbound_entrypoint_query)

        return inbound_workload, inbound_entrypoint


    def get_capacity_metrics(self):
        duration_sum_query = f"""
            sum(
                rate(istio_request_duration_milliseconds_sum{{
                    destination_workload="{self.service_name}",
                    reporter="destination"
                }}[{self.poll_interval}s])
            )
        """
        duration_count_query = f"""
            sum(
                rate(istio_request_duration_milliseconds_count{{
                    destination_workload="{self.service_name}",
                    reporter="destination"
                }}[{self.poll_interval}s])
            )
        """
        duration_sum_query_prev = f"""
            sum(
                rate(istio_request_duration_milliseconds_sum{{
                    destination_workload="{self.service_name}",
                    reporter="destination"
                }}[{self.poll_interval}s] offset {self.poll_interval}s)
            )
        """
        duration_count_query_prev = f"""
            sum(
                rate(istio_request_duration_milliseconds_count{{
                    destination_workload="{self.service_name}",
                    reporter="destination"
                }}[{self.poll_interval}s] offset {self.poll_interval}s)
            )
        """
        max_downstream_latency_query = f"""
        max(
            (
                sum by (destination_workload) (
                    rate(istio_request_duration_milliseconds_sum{{
                        source_workload="{self.service_name}",
                        reporter="source"
                    }}[{self.poll_interval}s])
                )
            )
            /
            (
                sum by (destination_workload) (
                    rate(istio_request_duration_milliseconds_count{{
                        source_workload="{self.service_name}",
                        reporter="source"
                    }}[{self.poll_interval}s])
                )
            )
        )
        """

        total_latency_s = self.query_prometheus(duration_sum_query) / 1000
        latency_elements = self.query_prometheus(duration_count_query)
        total_latency_s_prev = self.query_prometheus(duration_sum_query_prev) / 1000
        latency_elements_prev = self.query_prometheus(duration_count_query_prev)
        avg_total_latency, max_downstream, avg_total_latency_prev = None, None, None
        if latency_elements = 0:
            avg_total_latency = total_latency_s / latency_elements
            max_downstream = self.query_prometheus(max_downstream_latency_query) / 1000
            avg_service_time = max(avg_total_latency - max_downstream, 0.0)
            if latency_elements_prev != 0:
                avg_total_latency_prev = total_latency_s_prev / latency_elements_prev
        return avg_service_time, max_downstream, total_latency_s/avg_total_latency_prev


    # def get_metrics(self):

    #     cpu_usage_query = f"""rate(container_cpu_usage_seconds_total{{pod=~"{self.service_name}.*", pod!~".*autoscaler.*", container="server"}}[{self.poll_interval}s]) * 100000"""
    #     cpu_limit_query = f"""container_spec_cpu_quota{{pod=~"{self.service_name}.*", pod!~".*autoscaler.*", container="server"}}"""
    #     mem_usage_query = f"""avg(container_memory_working_set_bytes{{pod=~"{self.service_name}.*", pod!~".*autoscaler.*", container="server"}})"""
    #     mem_limit_query = f"""avg(container_spec_memory_limit_bytes{{pod=~"{self.service_name}.*", pod!~".*autoscaler.*", container="server"}})"""
    #     # --- Inbound throughput (req/s) ---


    #     # --- Outbound latency to downstreams (p95, ms) ---
    #     downstream_latency_query = f"""
    #     max(
    #         histogram_quantile(
    #             0.95,
    #             sum(
    #             rate(istio_request_duration_milliseconds_bucket{{
    #                 source_workload="{self.service_name}",
    #                 reporter="destination"
    #             }}[{self.poll_interval}s])
    #             ) by (le, destination_service)
    #         )
    #     )
    #     """

    #     # --- Total latency for requests from this service (p95, ms) ---
    #     total_latency_query = f"""
    #     histogram_quantile(
    #       0.95,
    #       sum(
    #         rate(istio_request_duration_milliseconds_bucket{{
    #           destination_workload="{self.service_name}",
    #           reporter="source"
    #         }}[{self.poll_interval}s])
    #       ) by (le)
    #     )
    #     """
    #     # ---Total requests entering the system ---

    #     successful_requests_query = f"""
    #         sum(
    #             rate(istio_requests_total{{
    #                 destination_workload="{self.service_name}",
    #                 reporter="source",
    #                 response_code!~"5.."
    #             }}[{self.poll_interval}s])
    #         )
    #     """

    #     retried_requests_query = f"""
    #     sum(
    #         rate(istio_requests_total{{
    #             destination_workload="{self.service_name}",
    #             reporter="destination",
    #             response_flags=~".*R.*"
    #         }}[{self.poll_interval}s])
    #     )
    #     """

    #     std_dev_query = f"""
    #     stddev_over_time(
    #         sum(rate(istio_requests_total{{
    #             destination_workload="{self.service_name}",
    #             reporter="source"
    #         }}[{self.poll_interval}s]))[3m:]
    #     )
    #     """
    
    #     # e2e_latency_s_query = f"""
    #     # histogram_quantile(
    #     #     0.95,
    #     #     sum(
    #     #         rate(istio_request_duration_milliseconds_bucket{{
    #     #         destination_workload="{self.entrypoint}",
    #     #         reporter="source"
    #     #         }}[{self.poll_interval}s])
    #     #     ) by (le)
    #     # )
    #     # """

    #     last_window_rps_query = f"""
    #         sum(
    #             rate(istio_requests_total{{
    #             destination_workload="{self.service_name}",
    #             reporter="source"
    #             }}[{self.poll_interval}s] offset {self.poll_interval}s)
    #         )
        
    #     """

    #     local_inbound_rps = self.query_prometheus(inbound_query)
    #     last_window_rps = self.query_prometheus(last_window_rps_query)
    #     entrypoint_rps = self.query_prometheus(inbound_entrypoint_query)
    #     total_latency_s = self.query_prometheus(total_latency_query) / 1000.0
    #     downstream_latency_s = self.query_prometheus(downstream_latency_query) / 1000.0
    #     # e2e_latency_s = self.query_prometheus(e2e_latency_s_query) / 1000.0
    #     successful_requests = self.query_prometheus(successful_requests_query)
    #     retried_requests = self.query_prometheus(retried_requests_query)
    #     std_dev = self.query_prometheus(std_dev_query)
    #     cpu_usage = self.query_prometheus(cpu_usage_query)
    #     cpu_limit = self.query_prometheus(cpu_limit_query)
    #     mem_usage = self.query_prometheus(mem_usage_query)
    #     mem_limit = self.query_prometheus(mem_limit_query)
    #     cpu_p = 0
    #     mem_p = 0 
    #     if cpu_limit > 0:
    #         cpu_p = cpu_usage/cpu_limit
    #     if mem_limit > 0:
    #         mem_p = mem_usage/mem_limit

    #     # Local tail latency = total - downstream
    #     local_latency_s = max(total_latency_s - downstream_latency_s, 0.0)
    #     if local_inbound_rps > 0:
    #         successful_rate = successful_requests / local_inbound_rps
    #         retry_rate = retried_requests / local_inbound_rps
    #         std_dev_workload = std_dev / local_inbound_rps
    #     else:
    #         successful_rate = 1.0
    #         retry_rate = 0.0
    #         std_dev_workload = 0.0
            
    #     if last_window_rps > 0:
    #         rps_delta_pct = (local_inbound_rps - last_window_rps) / last_window_rps
    #     else:
    #         rps_delta_pct = 0.0

    #     # return local_inbound_rps, entrypoint_rps, e2e_latency_s, local_latency_s, downstream_latency_s, successful_rate, retry_rate, std_dev_workload, cpu_p if cpu_p > 0 else None, mem_p if mem_p > 0  else None

    #     return local_inbound_rps, self.margin * entrypoint_rps, rps_delta_pct, local_latency_s, downstream_latency_s, successful_rate, retry_rate, std_dev_workload, cpu_p if cpu_p > 0 else None, mem_p if mem_p > 0  else None

    def run_loop(self):
        min_instances = self.get_current_replicas()
        instances = min_instances
        capacity = None
        fanout = None
        update_inst = False
        while True:
            # local_inbound_rps, entrypoint_rps, e2e_latency_s, local_latency_s, downstream_latency_s, \
            # local_inbound_rps, entrypoint_rps, rps_delta_pct, local_latency_s, downstream_latency_s, \
            # successful_rate, retry_rate, std_dev_workload, cpu_p, mem_p = self.get_metrics()
            local_inbound_rps, entrypoint_rps = self.get_load_metrics()
            avg_service_time, max_downstream, avg_total_latency_ratio = self.get_capacity_metrics()
            print(
                f"[{self.service_name}] "
                f"RPS_local={local_inbound_rps:.2f}, "
                f"RPS_entry={entrypoint_rps:.2f}, "
                f"E[S]={avg_service_time:.2f}s, "
                f"MAX(E[D])={max_downstream:.2f}s, "
                f"E[T]_t/E[T]_(t-1)={avg_total_latency_ratio:.2f}s, "
                f"INST={instances} "
            )
            # print(
            #     f"[{self.service_name}] "
            #     f"RPS_local={local_inbound_rps:.2f}, "
            #     f"RPS_entry={entrypoint_rps:.2f}, "
            #     # f"E2E_p95={e2e_latency_s:.2f}s, "
            #     f"Local_p95={local_latency_s:.2f}s, "
            #     f"Downstream_p95={downstream_latency_s:.2f}s, "
            #     f"SuccRate={successful_rate:.3f}, "
            #     f"RetryRate={retry_rate:.3f}, "
            #     f"WorkloadStd={std_dev_workload:.3f}, "
            #     f"{f'Cap={capacity:.2f}, ' if capacity else 'Cap=N/A, '}"
            #     f"{f'FOut={fanout:.2f}, ' if fanout else 'FOut=N/A, '}"
            #     f"{f'CPU={cpu_p:.2f}, ' if cpu_p is not None else 'CPU=N/A, '}"
            #     f"{f'MEM={mem_p:.2f}, ' if mem_p is not None else 'MEM=N/A, '}"
            #     f"INST={instances} "
            # )

            # self.cooldown = max(0,self.cooldown - 1)
            # if self.cooldown == 0:
            #     self.capacity_learner.update_metrics(request_rate=local_inbound_rps,local_latency_s=local_latency_s, cpu_p=cpu_p, mem_p=mem_p, current_replicas=instances, min_replicas=min_instances)
            #     self.fanout_learner.update_metrics(local_inbound_rps=local_inbound_rps, entrypoint_rps=entrypoint_rps, successful_rate=successful_rate, retry_rate=retry_rate, workload_stddev=std_dev_workload)
            # # --- Scaling decision ---
            # if self.cooldown == 0 or rps_delta_pct > 0 and rps_delta_pct < 0.05:
            #     capacity = self.capacity_learner.get_capacity()
            #     fanout = self.fanout_learner.get_fanout()
            #     saturated = local_latency_s > self.slo_s
            #     if capacity is not None and capacity > 0:
            #         if fanout is None:
            #             desired_replicas = max(min_instances, math.ceil(local_inbound_rps / capacity))
            #         else:
            #             desired_replicas = max(min_instances, math.ceil(fanout * entrypoint_rps / capacity))
            #         scale_condition = (desired_replicas > instances and saturated) or (desired_replicas < instances and not saturated)
            #         if scale_condition:
            #             print(f"[{self.service_name}] Scaling {instances} -> {desired_replicas} replicas")
            #             self.scale(desired_replicas)
            #             if desired_replicas > instances:                             
            #                 self.cooldown = self.cooldown_period  
            #                 self.capacity_learner.underload_episode_reset()                          
            #             update_inst = True
            time.sleep(self.poll_interval)
            if update_inst:
                instances = self.get_current_replicas()
                update_inst = False
            print(
                f"[{self.service_name}] "
                f"RPS_local={local_inbound_rps:.2f}, "
                f"RPS_entry={entrypoint_rps:.2f}, "
                # f"E2E_p95={e2e_latency_s:.2f}s, "
                f"Local_p95={local_latency_s:.2f}s, "
                f"Downstream_p95={downstream_latency_s:.2f}s, "
                f"SuccRate={successful_rate:.3f}, "
                f"RetryRate={retry_rate:.3f}, "
                f"WorkloadStd={std_dev_workload:.3f}, "
                f"{f'Cap={capacity:.2f}, ' if capacity else 'Cap=N/A, '}"
                f"{f'FOut={fanout:.2f}, ' if fanout else 'FOut=N/A, '}"
                f"{f'CPU={cpu_p:.2f}, ' if cpu_p is not None else 'CPU=N/A, '}"
                f"{f'MEM={mem_p:.2f}, ' if mem_p is not None else 'MEM=N/A, '}"
                f"INST={instances} "
            )

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
