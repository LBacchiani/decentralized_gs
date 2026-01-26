import os
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 45))
SERVICE_NAME = os.getenv("SERVICE_NAME")
if not SERVICE_NAME:
    raise ValueError("SERVICE_NAME must be set")

ENTRYPOINT = os.getenv("ENTRYPOINT")
if not ENTRYPOINT:
    raise ValueError("ENTRYPOINT must be set")


duration_sum_query = f"""
    sum(
        rate(istio_request_duration_milliseconds_sum{{
            destination_workload="{SERVICE_NAME}",
            reporter="destination"
        }}[{POLL_INTERVAL}s])
    )
"""
duration_count_query = f"""
    sum(
        rate(istio_request_duration_milliseconds_count{{
            destination_workload="{SERVICE_NAME}",
            reporter="destination"
        }}[{POLL_INTERVAL}s])
    )
"""
max_downstream_latency_query = f"""
max(
    (
        sum by (destination_workload) (
            rate(istio_request_duration_milliseconds_sum{{
                source_workload="{SERVICE_NAME}",
                reporter="destination"
            }}[{POLL_INTERVAL}s])
        )
    )
    /
    (
        sum by (destination_workload) (
            rate(istio_request_duration_milliseconds_count{{
                source_workload="{SERVICE_NAME}",
                reporter="destination"
            }}[{POLL_INTERVAL}s])
        )
    )
)
"""
cpu_usage_query = f"""sum(
    rate(container_cpu_usage_seconds_total{{
        namespace="default",
        pod=~"{SERVICE_NAME}-.*",
        container!="istio-proxy",
        container!=""
        }}[{POLL_INTERVAL}s]
    )
)
"""
inbound_query = f"""
    sum(
    rate(istio_requests_total{{
        destination_workload="{SERVICE_NAME}",
        reporter="destination"
    }}[{POLL_INTERVAL}s])
    )
"""
inbound_entrypoint_query = f"""
    sum(
        rate(istio_requests_total{{
            destination_workload="{ENTRYPOINT}",
            reporter="destination"
        }}[{POLL_INTERVAL}s])
    )
"""
retried_requests_query = f"""
    sum(
        rate(istio_requests_total{{
            destination_workload="{SERVICE_NAME}",
            reporter="destination",
            response_flags=~".*R.*"
        }}[{POLL_INTERVAL}s])
    )
"""


successful_requests_query = f"""
    sum(
        rate(istio_requests_total{{
            destination_workload="{SERVICE_NAME}",
            reporter="destination",
            response_code!~"2.."
        }}[{POLL_INTERVAL}s])
    )
"""

cpu_period_query = f"""avg(container_spec_cpu_period{{pod=~"{SERVICE_NAME}.*", pod!~"{SERVICE_NAME}-autoscaler.*"}})"""
cpu_spec_total_quota_query = f"""avg(container_spec_cpu_quota{{pod=~"{SERVICE_NAME}.*", pod!~"{SERVICE_NAME}-autoscaler.*", container=""}})"""
cpu_spec_istio_quota_query = f"""avg(container_spec_cpu_quota{{pod=~"{SERVICE_NAME}.*", pod!~"{SERVICE_NAME}-autoscaler.*", container="istio-proxy"}})"""

p99_query = f'''
        histogram_quantile(
          0.99,
          sum(rate(istio_request_duration_milliseconds_bucket{{
            destination_workload="{SERVICE_NAME}",
            reporter="destination",
            response_code=~"2.."
          }}[{POLL_INTERVAL}])) by (le)
        ) / 1000
'''
