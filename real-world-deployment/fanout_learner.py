import math
from collections import deque
import os

class ServiceFanoutLearner:
    def __init__(self, service_name, local_slo_s, poll_interval):
        self.service = service_name
        self.poll_interval = poll_interval

        # parameters
        self.local_slo_s = local_slo_s
        self.global_slo_s = float(os.getenv("GLOBAL_SLO", 1500)) / 1000
        self.windows_required = int(os.getenv("PLATEAU_WINDOWS", 5))

        # fanout tracking
        self.fanout_history = deque(maxlen=50)
        self.fanout = None
        print(f"[{self.service}] Fanout learner initialized")

    def update_metrics(
        self,
        local_inbound_rps: float, entrypoint_rps: float,
        e2e_latency_s: float, local_latency_s: float,
        retry_rate: float, workload_stddev: float
    ):
        # --- basic guards ---
        if entrypoint_rps <= 0 or local_inbound_rps <= 0:
            return

        # --- steady-state detection ---
        steady = e2e_latency_s < self.global_slo_s and local_latency_s < self.local_slo_s and retry_rate < 0.05 and workload_stddev < 0.1
        

        if not steady:
            return

        # --- compute instantaneous fanout ---
        fanout_sample = local_inbound_rps / entrypoint_rps
        self.fanout_history.append(fanout_sample)
        self._compute_fanout()


    def _compute_fanout(self):
        n = len(self.fanout_history)
        if n == 0:
            return

        if n < 5:
            self.fanout = sum(self.fanout_history) / n
        else:
            sorted_samples = sorted(self.fanout_history)
            trim = math.floor(0.25 * n)
            middle = sorted_samples[trim : n - trim]

            smoothed = sum(middle) / len(middle)
            print(f"[{self.service}] SMOOTHED FANOUT={smoothed:.3f}")
            self.fanout = smoothed

        print(f"[{self.service}] *** Fanout updated: {self.fanout:.3f} ***")

    def get_fanout(self):
        return self.fanout
