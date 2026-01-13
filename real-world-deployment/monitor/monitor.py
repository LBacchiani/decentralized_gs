import os
import time
import requests
import logging
from collections import deque
from statistics import variance
from datetime import datetime

# ----------------------------
# Configuration
# ----------------------------
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "45"))
PROMETHEUS = os.getenv(
    "PROMETHEUS_TARGET",
    "http://prometheus.monitoring.svc.cluster.local:9090"
)

NAMESPACE = os.getenv("NAMESPACE", "default")

DEPLOYMENTS = os.getenv(
    "DEPLOYMENTS",
    "frontend|cartservice|productcatalogservice|currencyservice|paymentservice|shippingservice|emailservice|checkoutservice|recommendationservice|adservice"
)

FRONTEND_SERVICE = "frontend"

WORKLOAD_DELTA_THRESHOLD = float(os.getenv("WORKLOAD_DELTA_THRESHOLD", "0.3"))
WINDOW_SIZE = int(os.getenv("WINDOW_SIZE", "10"))

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ----------------------------
# Prometheus helpers
# ----------------------------
def promql(query: str):
    r = requests.get(
        f"{PROMETHEUS}/api/v1/query",
        params={"query": query},
        timeout=10
    )
    r.raise_for_status()
    return r.json()["data"]["result"]

def scalar(query: str) -> float:
    result = promql(query)
    if not result:
        return 0.0
    return float(result[0]["value"][1])

# ----------------------------
# Online Boutique metrics
# ----------------------------
def inbound_rps():
    return scalar(
        f'''
        sum(
          rate(istio_requests_total{{
            destination_app="{FRONTEND_SERVICE}",
            reporter="destination"
          }}[1m])
        )
        '''
    )

def error_rate():
    return scalar(
        f'''
        sum(
          rate(istio_requests_total{{
            destination_app="{FRONTEND_SERVICE}",
            reporter="destination",
            response_code=~"5.."
          }}[{POLL_INTERVAL}])
        )
        '''
    )

def latency_quantile(q: float):
    return scalar(
        f'''
        histogram_quantile(
          {q},
          sum(
            rate(istio_request_duration_milliseconds_bucket{{
            destination_app="{FRONTEND_SERVICE}",
              reporter="destination"
            }}[{POLL_INTERVAL}])
          ) by (le)
        ) / 1000
        '''
    )

def total_replicas():
    return scalar(
        f'''
        sum(
          kube_deployment_status_replicas{{
            namespace="{NAMESPACE}",
            deployment=~"{DEPLOYMENTS}"
          }}
        )
        '''
    )

# ----------------------------
# State tracking
# ----------------------------
rps_history = deque(maxlen=WINDOW_SIZE)
replica_history = deque(maxlen=WINDOW_SIZE)

pending_scale_event_ts = None
scale_reaction_delays = []
scaling_events = 0

# ----------------------------
# Main loop
# ----------------------------
def main():
    global pending_scale_event_ts, scaling_events

    logging.info("Panscaler monitor started")

    while True:
        now = time.time()
        ts = datetime.utcnow().isoformat()

        rps = inbound_rps()
        replicas = total_replicas()

        rps_history.append(rps)
        replica_history.append(replicas)

        # Detect workload spike
        if len(rps_history) >= 2:
            prev = rps_history[-2]
            if prev > 0:
                delta = (rps - prev) / prev
                if delta > WORKLOAD_DELTA_THRESHOLD and pending_scale_event_ts is None:
                    pending_scale_event_ts = now
                    logging.info(f"Workload increase detected (Δ={delta:.2f})")

        # Detect scaling event
        if len(replica_history) >= 2:
            if replica_history[-1] != replica_history[-2]:
                scaling_events += 1
                logging.info(
                    f"Scaling event: replicas {replica_history[-2]} → {replica_history[-1]}"
                )

                if pending_scale_event_ts is not None:
                    delay = now - pending_scale_event_ts
                    scale_reaction_delays.append(delay)
                    logging.info(f"Scale reaction delay = {delay:.2f}s")
                    pending_scale_event_ts = None

        # Stability metric
        rep_var = variance(replica_history) if len(replica_history) > 1 else 0.0

        # Performance metrics
        p50 = latency_quantile(0.50)
        p90 = latency_quantile(0.90)
        p99 = latency_quantile(0.99)
        err = error_rate()
        throughput = max(rps - err, 0.0)

        logging.info(
            f"[{ts}] "
            f"RPS={rps:.2f} "
            f"THR={throughput:.2f} "
            f"ERR={err:.2f} "
            f"p50={p50:.3f}s p90={p90:.3f}s p99={p99:.3f}s "
            f"total_replicas={replicas} "
            f"replica_var={rep_var:.2f} "
            f"scale_events={scaling_events}"
        )

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
