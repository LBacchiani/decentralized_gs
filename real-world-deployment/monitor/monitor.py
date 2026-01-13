import os
import time
import requests
import logging
import json
from collections import deque
from statistics import variance, mean
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
SLO_LATENCY_MS = float(os.getenv("SLO_LATENCY_MS", "500"))
WORKLOAD_DELTA_THRESHOLD = float(os.getenv("WORKLOAD_DELTA_THRESHOLD", "0.3"))
WINDOW_SIZE = int(os.getenv("WINDOW_SIZE", "10"))

# Cost estimation (AWS t3.medium pricing)
COST_PER_POD_HOUR = float(os.getenv("COST_PER_POD_HOUR", "0.00416"))  # $0.0416/hr / 10 pods

# Output configuration
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/data")
EXPERIMENT_NAME = os.getenv("EXPERIMENT_NAME", "experiment")

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ----------------------------
# Data Export
# ----------------------------
class DataExporter:
    """Exports metrics in multiple formats for graph generation"""
    
    def __init__(self, output_dir, experiment_name):
        self.output_dir = output_dir
        self.experiment_name = experiment_name
        self.timeseries_data = []
        self.service_data = []
        self.events_data = []
        self.summary_data = {}
        
        # Create output directory
        os.makedirs(output_dir, exist_ok=True)
        
        # Initialize CSV files
        self.timeseries_file = open(
            f"{output_dir}/{experiment_name}_timeseries.csv", "w"
        )
        self.timeseries_file.write(
            "timestamp,elapsed_seconds,rps,error_rate,total_replicas,"
            "p50_latency,p90_latency,p95_latency,p99_latency,"
            "slo_violation_pct,slo_compliance_pct,pod_hours,cost,"
            "scaling_events,oscillations\n"
        )
        
        self.service_file = open(
            f"{output_dir}/{experiment_name}_services.csv", "w"
        )
        self.service_file.write(
            "timestamp,elapsed_seconds,service,rps,p95_latency,replicas,cpu_cores\n"
        )
        
        self.events_file = open(
            f"{output_dir}/{experiment_name}_events.csv", "w"
        )
        self.events_file.write(
            "timestamp,elapsed_seconds,event_type,details\n"
        )
        
        logging.info(f"📁 Data export initialized: {output_dir}/{experiment_name}_*")
    
    def record_timeseries(self, ts, elapsed, metrics):
        """Record time-series data point"""
        # Write to CSV
        self.timeseries_file.write(
            f"{ts},{elapsed:.2f},{metrics['rps']:.2f},{metrics['error_rate']:.2f},"
            f"{metrics['total_replicas']:.0f},"
            f"{metrics['p50']:.4f},{metrics['p90']:.4f},{metrics['p95']:.4f},{metrics['p99']:.4f},"
            f"{metrics['slo_violation_pct']:.2f},{metrics['slo_compliance_pct']:.2f},"
            f"{metrics['pod_hours']:.2f},{metrics['cost']:.4f},"
            f"{metrics['scaling_events']},{metrics['oscillations']}\n"
        )
        self.timeseries_file.flush()
        
        # Store in memory for JSON export
        self.timeseries_data.append({
            'timestamp': ts,
            'elapsed_seconds': elapsed,
            **metrics
        })
    
    def record_service_metrics(self, ts, elapsed, service, metrics):
        """Record per-service metrics"""
        # Write to CSV
        self.service_file.write(
            f"{ts},{elapsed:.2f},{service},"
            f"{metrics['rps']:.2f},{metrics['p95_latency']:.4f},"
            f"{metrics['replicas']},{metrics['cpu_cores']:.4f}\n"
        )
        self.service_file.flush()
        
        # Store in memory
        self.service_data.append({
            'timestamp': ts,
            'elapsed_seconds': elapsed,
            'service': service,
            **metrics
        })
    
    def record_event(self, ts, elapsed, event_type, details):
        """Record scaling/workload events"""
        # Write to CSV
        self.events_file.write(
            f"{ts},{elapsed:.2f},{event_type},\"{details}\"\n"
        )
        self.events_file.flush()
        
        # Store in memory
        self.events_data.append({
            'timestamp': ts,
            'elapsed_seconds': elapsed,
            'event_type': event_type,
            'details': details
        })
        
        logging.info(f"📌 EVENT: {event_type} - {details}")
    
    def update_summary(self, summary):
        """Update summary statistics"""
        self.summary_data = summary
    
    def export_json(self):
        """Export all data as JSON for flexible post-processing"""
        output = {
            'experiment_name': self.experiment_name,
            'configuration': {
                'slo_latency_ms': SLO_LATENCY_MS,
                'poll_interval': POLL_INTERVAL,
                'namespace': NAMESPACE,
                'services': DEPLOYMENTS.split("|")
            },
            'timeseries': self.timeseries_data,
            'services': self.service_data,
            'events': self.events_data,
            'summary': self.summary_data
        }
        
        json_file = f"{self.output_dir}/{self.experiment_name}_complete.json"
        with open(json_file, 'w') as f:
            json.dump(output, f, indent=2)
        
        logging.info(f"💾 JSON export complete: {json_file}")
    
    def export_summary(self):
        """Export summary statistics"""
        summary_file = f"{self.output_dir}/{self.experiment_name}_summary.txt"
        with open(summary_file, 'w') as f:
            f.write("=" * 80 + "\n")
            f.write(f"EXPERIMENT SUMMARY: {self.experiment_name}\n")
            f.write("=" * 80 + "\n")
            for key, value in self.summary_data.items():
                f.write(f"{key}: {value}\n")
            f.write("=" * 80 + "\n")
        
        logging.info(f"📊 Summary export complete: {summary_file}")
    
    def close(self):
        """Close all file handles and export final data"""
        self.timeseries_file.close()
        self.service_file.close()
        self.events_file.close()
        self.export_json()
        self.export_summary()
        logging.info("✅ All data exported successfully")

# ----------------------------
# Prometheus helpers
# ----------------------------
def promql(query: str):
    try:
        r = requests.get(
            f"{PROMETHEUS}/api/v1/query",
            params={"query": query},
            timeout=10
        )
        r.raise_for_status()
        return r.json()["data"]["result"]
    except Exception as e:
        logging.error(f"Prometheus query failed: {e}")
        return []

def scalar(query: str) -> float:
    result = promql(query)
    if not result:
        return 0.0
    return float(result[0]["value"][1])

# ----------------------------
# Global metrics
# ----------------------------
def inbound_rps():
    return scalar(
        f'''
        sum(
          rate(istio_requests_total{{
            destination_app="{FRONTEND_SERVICE}",
            reporter="destination"
          }}[{POLL_INTERVAL}s])
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
          }}[{POLL_INTERVAL}s])
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
            }}[{POLL_INTERVAL}s])
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
# SLO Compliance (CRITICAL)
# ----------------------------
def slo_violation_rate():
    """Percentage of requests violating SLO"""
    # Total requests
    total = scalar(
        f'''
        sum(
          rate(istio_requests_total{{
            destination_app="{FRONTEND_SERVICE}",
            reporter="destination"
          }}[1m])
        )
        '''
    )
    
    if total == 0:
        return 0.0
    
    # Requests meeting SLO (latency <= SLO_LATENCY_MS)
    meeting_slo = scalar(
        f'''
        sum(
          rate(istio_request_duration_milliseconds_bucket{{
            destination_app="{FRONTEND_SERVICE}",
            reporter="destination",
            le="{SLO_LATENCY_MS}"
          }}[1m])
        )
        '''
    )
    
    # Violation rate
    violations = max(0, total - meeting_slo)
    return (violations / total) * 100

# ----------------------------
# Per-Service Metrics
# ----------------------------
def per_service_metrics():
    """Get detailed metrics for each service"""
    services = [s.strip() for s in DEPLOYMENTS.split("|")]
    metrics = {}
    
    for svc in services:
        try:
            rps = scalar(
                f'''
                sum(
                  rate(istio_requests_total{{
                    destination_app="{svc}",
                    reporter="destination"
                  }}[{POLL_INTERVAL}s])
                )
                '''
            )
            
            p95 = scalar(
                f'''
                histogram_quantile(
                  0.95,
                  sum(
                    rate(istio_request_duration_milliseconds_bucket{{
                      destination_app="{svc}",
                      reporter="destination"
                    }}[{POLL_INTERVAL}s])
                  ) by (le)
                ) / 1000
                '''
            )
            
            replicas = scalar(
                f'''
                kube_deployment_status_replicas{{
                  namespace="{NAMESPACE}",
                  deployment="{svc}"
                }}
                '''
            )
            
            cpu_usage = scalar(
                f'''
                sum(
                  rate(container_cpu_usage_seconds_total{{
                    namespace="{NAMESPACE}",
                    pod=~"{svc}.*",
                    container!="POD",
                    container!=""
                  }}[{POLL_INTERVAL}s])
                )
                '''
            )
            
            metrics[svc] = {
                'rps': rps,
                'p95_latency': p95,
                'replicas': int(replicas),
                'cpu_cores': cpu_usage
            }
        except Exception as e:
            logging.debug(f"Failed to get metrics for {svc}: {e}")
            metrics[svc] = {
                'rps': 0.0,
                'p95_latency': 0.0,
                'replicas': 0,
                'cpu_cores': 0.0
            }
    
    return metrics

# ----------------------------
# Resource Efficiency
# ----------------------------
def resource_efficiency(elapsed_hours):
    """Calculate pod-hours and estimated cost"""
    current_replicas = total_replicas()
    
    # Pod-hours = replicas × time
    pod_hours = current_replicas * elapsed_hours
    
    # Estimated cost
    cost = pod_hours * COST_PER_POD_HOUR
    
    return pod_hours, cost

# ----------------------------
# Stability Metrics
# ----------------------------
def stability_metrics(replica_history, scaling_events, elapsed_time):
    """Calculate stability indicators"""
    
    # Oscillation detection: up-down-up or down-up-down patterns
    oscillations = 0
    if len(replica_history) >= 3:
        for i in range(len(replica_history) - 2):
            if (replica_history[i] < replica_history[i+1] > replica_history[i+2]) or \
               (replica_history[i] > replica_history[i+1] < replica_history[i+2]):
                oscillations += 1
    
    # Scaling frequency (events per hour)
    scaling_frequency = (scaling_events / elapsed_time * 3600) if elapsed_time > 0 else 0.0
    
    # Replica variance
    rep_variance = variance(replica_history) if len(replica_history) > 1 else 0.0
    
    # Average replicas
    avg_replicas = mean(replica_history) if replica_history else 0.0
    
    return {
        'oscillations': oscillations,
        'scaling_frequency': scaling_frequency,
        'replica_variance': rep_variance,
        'avg_replicas': avg_replicas
    }

# ----------------------------
# State tracking
# ----------------------------
rps_history = deque(maxlen=WINDOW_SIZE)
replica_history = deque(maxlen=WINDOW_SIZE)
slo_violation_history = deque(maxlen=WINDOW_SIZE)

pending_scale_event_ts = None
scale_reaction_delays = []
scaling_events = 0

slo_violations_cumulative = 0.0
measurements_total = 0

start_time = None

# Workload spike tracking
workload_spike_detected_ts = None
slo_restored_ts = None
time_to_slo_restoration = None

# ----------------------------
# Main loop
# ----------------------------
def main():
    global pending_scale_event_ts, scaling_events, start_time
    global slo_violations_cumulative, measurements_total
    global workload_spike_detected_ts, slo_restored_ts, time_to_slo_restoration

    logging.info("=" * 80)
    logging.info("Panscaler Evaluation Monitor Started")
    logging.info("=" * 80)
    logging.info(f"Prometheus: {PROMETHEUS}")
    logging.info(f"Namespace: {NAMESPACE}")
    logging.info(f"Services: {DEPLOYMENTS}")
    logging.info(f"SLO Latency: {SLO_LATENCY_MS}ms")
    logging.info(f"Poll Interval: {POLL_INTERVAL}s")
    logging.info(f"Output Directory: {OUTPUT_DIR}")
    logging.info(f"Experiment Name: {EXPERIMENT_NAME}")
    logging.info("=" * 80)

    # Initialize data exporter
    exporter = DataExporter(OUTPUT_DIR, EXPERIMENT_NAME)

    start_time = time.time()

    try:
        while True:
            try:
                now = time.time()
                elapsed_time = now - start_time
                elapsed_hours = elapsed_time / 3600.0
                ts = datetime.utcnow().isoformat()

                # ==================== Basic Metrics ====================
                rps = inbound_rps()
                replicas = total_replicas()
                err = error_rate()
                
                rps_history.append(rps)
                replica_history.append(replicas)

                # ==================== SLO Compliance ====================
                slo_violation_pct = slo_violation_rate()
                slo_violation_history.append(slo_violation_pct)
                slo_violations_cumulative += slo_violation_pct
                measurements_total += 1
                
                avg_slo_compliance = (1 - slo_violations_cumulative / measurements_total) * 100 if measurements_total > 0 else 100.0

                # ==================== Latency ====================
                p50 = latency_quantile(0.50)
                p90 = latency_quantile(0.90)
                p95 = latency_quantile(0.95)
                p99 = latency_quantile(0.99)

                # ==================== Workload Spike Detection ====================
                if len(rps_history) >= 2:
                    prev_rps = rps_history[-2]
                    if prev_rps > 0:
                        delta = (rps - prev_rps) / prev_rps
                        if delta > WORKLOAD_DELTA_THRESHOLD and workload_spike_detected_ts is None:
                            workload_spike_detected_ts = now
                            slo_restored_ts = None
                            logging.info(f"🔥 WORKLOAD SPIKE DETECTED (Δ={delta:.2%})")
                            exporter.record_event(
                                ts, elapsed_time, "WORKLOAD_SPIKE",
                                f"RPS increased by {delta:.1%} from {prev_rps:.1f} to {rps:.1f}"
                            )

                # ==================== SLO Restoration Detection ====================
                if workload_spike_detected_ts is not None and slo_restored_ts is None:
                    # Check if SLO is restored (violations < 5% for stability)
                    if slo_violation_pct < 5.0:
                        slo_restored_ts = now
                        time_to_slo_restoration = slo_restored_ts - workload_spike_detected_ts
                        logging.info(f"✅ SLO RESTORED in {time_to_slo_restoration:.1f}s")
                        exporter.record_event(
                            ts, elapsed_time, "SLO_RESTORED",
                            f"SLO restored after {time_to_slo_restoration:.1f}s"
                        )

                # ==================== Scaling Event Detection ====================
                if len(replica_history) >= 2:
                    if replica_history[-1] != replica_history[-2]:
                        scaling_events += 1
                        direction = "UP" if replica_history[-1] > replica_history[-2] else "DOWN"
                        change = replica_history[-1] - replica_history[-2]
                        logging.info(
                            f"📊 SCALING EVENT #{scaling_events}: {replica_history[-2]:.0f} → {replica_history[-1]:.0f} ({direction}, {change:+.0f})"
                        )
                        exporter.record_event(
                            ts, elapsed_time, f"SCALING_{direction}",
                            f"Replicas: {replica_history[-2]:.0f} → {replica_history[-1]:.0f} ({change:+.0f})"
                        )

                        if pending_scale_event_ts is not None:
                            delay = now - pending_scale_event_ts
                            scale_reaction_delays.append(delay)
                            logging.info(f"⏱️  Scale reaction delay: {delay:.1f}s")
                            pending_scale_event_ts = None

                # ==================== Per-Service Metrics ====================
                service_metrics = per_service_metrics()

                # ==================== Resource Efficiency ====================
                pod_hours, estimated_cost = resource_efficiency(elapsed_hours)

                # ==================== Stability ====================
                stability = stability_metrics(list(replica_history), scaling_events, elapsed_time)

                # ==================== Export Time-Series Data ====================
                exporter.record_timeseries(ts, elapsed_time, {
                    'rps': rps,
                    'error_rate': err,
                    'total_replicas': replicas,
                    'p50': p50,
                    'p90': p90,
                    'p95': p95,
                    'p99': p99,
                    'slo_violation_pct': slo_violation_pct,
                    'slo_compliance_pct': avg_slo_compliance,
                    'pod_hours': pod_hours,
                    'cost': estimated_cost,
                    'scaling_events': scaling_events,
                    'oscillations': stability['oscillations']
                })

                # ==================== Export Per-Service Data ====================
                for svc, m in service_metrics.items():
                    exporter.record_service_metrics(ts, elapsed_time, svc, m)

                # ==================== Summary Logging ====================
                logging.info(
                    f"[{ts}] "
                    f"RPS={rps:.1f} "
                    f"ERR={err:.2f} "
                    f"Replicas={replicas:.0f} "
                    f"SLO_viol={slo_violation_pct:.1f}% "
                    f"Avg_SLO={avg_slo_compliance:.1f}% "
                    f"p50/p90/p95/p99={p50:.3f}/{p90:.3f}/{p95:.3f}/{p99:.3f}s "
                    f"PodH={pod_hours:.1f} "
                    f"Cost=${estimated_cost:.2f} "
                    f"ScaleEvents={scaling_events} "
                    f"Osc={stability['oscillations']} "
                    f"ScaleFreq={stability['scaling_frequency']:.1f}/h"
                )

                # ==================== Periodic Summary ====================
                if measurements_total > 0 and measurements_total % 20 == 0:
                    logging.info("=" * 80)
                    logging.info(f"📈 SUMMARY @ {elapsed_time/60:.1f} minutes")
                    logging.info("=" * 80)
                    logging.info(f"  Average SLO Compliance: {avg_slo_compliance:.2f}%")
                    logging.info(f"  Total Pod-Hours: {pod_hours:.1f}")
                    logging.info(f"  Estimated Cost: ${estimated_cost:.2f}")
                    logging.info(f"  Scaling Events: {scaling_events}")
                    logging.info(f"  Oscillations: {stability['oscillations']}")
                    logging.info(f"  Avg Replicas: {stability['avg_replicas']:.1f}")
                    logging.info(f"  Replica Variance: {stability['replica_variance']:.2f}")
                    
                    if scale_reaction_delays:
                        avg_delay = mean(scale_reaction_delays)
                        logging.info(f"  Avg Scale Reaction Delay: {avg_delay:.1f}s")
                    
                    if time_to_slo_restoration is not None:
                        logging.info(f"  Time to SLO Restoration: {time_to_slo_restoration:.1f}s")
                    
                    logging.info("=" * 80)

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                logging.error(f"Error in main loop: {e}")
                time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logging.info("Shutting down...")

    # ==================== Final Summary ====================
    elapsed_time = time.time() - start_time
    elapsed_hours = elapsed_time / 3600.0
    pod_hours, estimated_cost = resource_efficiency(elapsed_hours)
    stability = stability_metrics(list(replica_history), scaling_events, elapsed_time)

    summary = {
        'total_runtime_minutes': elapsed_time / 60,
        'average_slo_compliance_pct': avg_slo_compliance,
        'total_pod_hours': pod_hours,
        'estimated_cost_usd': estimated_cost,
        'total_scaling_events': scaling_events,
        'oscillations': stability['oscillations'],
        'average_replicas': stability['avg_replicas'],
        'replica_variance': stability['replica_variance'],
        'scaling_frequency_per_hour': stability['scaling_frequency'],
        'avg_scale_reaction_delay_s': mean(scale_reaction_delays) if scale_reaction_delays else None,
        'min_scale_reaction_delay_s': min(scale_reaction_delays) if scale_reaction_delays else None,
        'max_scale_reaction_delay_s': max(scale_reaction_delays) if scale_reaction_delays else None,
        'time_to_slo_restoration_s': time_to_slo_restoration,
        'slo_latency_threshold_ms': SLO_LATENCY_MS
    }

    exporter.update_summary(summary)

    logging.info("=" * 80)
    logging.info("📊 FINAL EVALUATION SUMMARY")
    logging.info("=" * 80)
    for key, value in summary.items():
        logging.info(f"  {key}: {value}")
    logging.info("=" * 80)

    # Close exporter and save all data
    exporter.close()

if __name__ == "__main__":
    main()