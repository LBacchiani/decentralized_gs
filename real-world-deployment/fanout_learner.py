import math
from collections import deque
import os

class ServiceFanoutLearner:
    def __init__(self, service_name):
        self.service = service_name

        # fanout tracking
        self.fanout_history = deque(maxlen=50)
        self.fanout = None
        print(f"[{self.service}] Fanout learner initialized")

    def update_metrics(self, local_inbound_rps: float, entrypoint_rps: float, successful_rate: float, retry_rate: float, workload_stddev: float):
        # --- basic guards ---
        if entrypoint_rps <= 0 or local_inbound_rps <= 0:
            return

        # --- steady-state detection ---
        steady = successful_rate > 0.999 and retry_rate < 0.05 and workload_stddev < 0.1
        

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
