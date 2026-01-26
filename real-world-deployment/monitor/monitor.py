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

WINDOW_SIZE = int(os.getenv("WINDOW_SIZE", "20"))
LATENCY_SLO = float(os.getenv("LATENCY_SLO", "1.5"))  # seconds (p99)

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
# Metrics
# ----------------------------
def inbound_rps():
    return scalar(
        f'''
        sum(rate(istio_requests_total{{
            destination_app="{FRONTEND_SERVICE}",
            reporter="destination"
        }}[1m]))
        '''
    )

def error_ratio():
    errors = scalar(
        f'''
        sum(rate(istio_requests_total{{
            destination_app="{FRONTEND_SERVICE}",
            reporter="destination",
            response_code=~"5.."
        }}[1m]))
        '''
    )
    total = inbound_rps()
    return errors / total if total > 0 else 0.0

def latency_p99():
    return scalar(
        f'''
        histogram_quantile(
          0.99,
          sum(rate(istio_request_duration_milliseconds_bucket{{
            destination_app="{FRONTEND_SERVICE}",
            reporter="destination"
          }}[1m])) by (le)
        ) / 1000
        '''
    )

def total_replicas():
    return scalar(
        f'''
        sum(kube_deployment_status_replicas{{
            namespace="{NAMESPACE}",
            deployment=~"{DEPLOYMENTS}"
        }})
        '''
    )

# ----------------------------
# State
# ----------------------------
replica_hist = deque(maxlen=WINDOW_SIZE)
p99_hist = deque(maxlen=WINDOW_SIZE)

scale_events = 0
oscillations = 0

last_replica_delta = 0

slo_recovery_start = None
slo_recovery_times = []

replica_seconds = 0.0
sample_count = 0

# ----------------------------
# Main loop
# ----------------------------
def main():
    global scale_events, oscillations, last_replica_delta
    global slo_recovery_start, replica_seconds, sample_count

    logging.info("Evaluation monitor started")

    while True:
        ts = datetime.utcnow().isoformat()

        rps = inbound_rps()
        replicas = total_replicas()
        p99 = latency_p99()
        err = error_ratio()

        replica_hist.append(replicas)
        p99_hist.append(p99)

        # ----------------------------
        # Scaling dynamics
        # ----------------------------
        if len(replica_hist) >= 2:
            delta = replica_hist[-1] - replica_hist[-2]
            if delta != 0:
                scale_events += 1
                if last_replica_delta != 0 and delta * last_replica_delta < 0:
                    oscillations += 1
                last_replica_delta = delta

        # ----------------------------
        # SLO analysis
        # ----------------------------


        logging.info(
            f"[{ts}] "
            f"RPS={rps:.2f} "
            f"ERR={err:.3f} "
            f"p99={p99:.3f}s "
            f"replicas={replicas:.1f} "
            f"scale_events={scale_events} "
            f"oscillations={oscillations} "
        )

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
