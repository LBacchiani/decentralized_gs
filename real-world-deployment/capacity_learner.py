import math
from collections import deque
import os

class ServiceCapacityLearner:
    def __init__(self, service_name, slo_s):
        self.service = service_name

        # parameters
        self.slo_s = slo_s
        self.windows_required = int(os.getenv("PLATEAU_WINDOWS", 5))

        # capacity tracking
        self.samples = int(os.getenv("HISTORY", 30))
        self.capacity_history = deque(maxlen=self.samples)
        self.capacity = None

        # rolling sample windows
        self.overload_samples = deque(maxlen=self.windows_required)
        self.underload_samples = deque(maxlen=self.windows_required)

        self.underload_episode = False
        self.load_reset_counter_limit = int(os.getenv("STABILITY_RESET", 3))
        self.load_reset_counter = 0

        print(f"[{self.service}] Capacity learner initialized")

    def update_metrics(
        self, request_rate: float, queue_delay_s: float, cpu_p: float, mem_p: float,  current_replicas: int, min_replicas: int):
        # --- sanitize cpu/mem fractions ---
        cpu_p = cpu_p if cpu_p is not None and cpu_p > 0 else 1
        mem_p = mem_p if mem_p is not None and mem_p > 0 else 1


        # --- detect overload ---
        saturated = queue_delay_s > self.slo_s

        if saturated:
            print(f"[{self.service}] *****OVERLOAD DETECTED*****")
            self.overload_samples.append((request_rate, queue_delay_s))
            if len(self.overload_samples) >= self.windows_required:
                smoothed_mu = self._compute_smoothed_capacity(self.overload_samples, current_replicas)
                self.capacity_history.append(smoothed_mu)
                self.overload_samples.clear()
                self._compute_capacity()

        
        # --- detect underload ---
        underload = not saturated and cpu_p < 0.5 and mem_p < 0.5 and current_replicas > min_replicas and not self.underload_episode
        if underload:
            print(f"[{self.service}] *****UNDERLOAD DETECTED*****")
            self.underload_samples.append((request_rate, queue_delay_s))
            if len(self.underload_samples) >= self.windows_required:
                smoothed_mu = self._compute_smoothed_capacity(self.underload_samples, max(current_replicas - 1, min_replicas))
                self.capacity_history.append(smoothed_mu)
                self.underload_samples.clear()
                self._compute_capacity()
                self.underload_episode = True

        stable = not saturated and not underload

        if stable:
            self.load_reset_counter += 1
        else:
            self.load_reset_counter = 0

        if self.load_reset_counter >= self.load_reset_counter_limit:
            self.underload_samples.clear()
            self.overload_samples.clear()
            self.load_reset_counter = 0



    def _compute_smoothed_capacity(self, samples, replica_factor):
        """
        Compute smoothed capacity from a list of (req_rate, latency) samples.
        Uses trimmed mean of upper 75%.
        """
        sorted_samples = sorted(samples, key=lambda x: x[1])
        trim = math.floor(0.25 * len(sorted_samples))
        upper = sorted_samples[trim:]
        
        smoothed_latency = sum(l for _, l in upper) / len(upper)
        smoothed_rate = sum(r for r, _ in upper) / len(upper)
        smoothed_replicas = max(1, math.ceil(replica_factor * (smoothed_latency / self.slo_s) ** 0.5))
        smoothed_mu = smoothed_rate / smoothed_replicas

        print(f"[{self.service}] SMOOTHED LATENCY={smoothed_latency:.3f}s, "
              f"SMOOTHED REPLICAS={smoothed_replicas}, SMOOTHED CAPACITY={smoothed_mu:.2f} req/s/replica")
        return smoothed_mu

    def _compute_capacity(self):
        n = len(self.capacity_history)
        if n == 0:
            return

        if n < 5:
            self.capacity = sum(self.capacity_history) / n
        else:
            sorted_caps = sorted(self.capacity_history)
            trim = math.floor(0.25 * n)
            upper = sorted_caps[trim:]
            self.capacity = sum(upper) / len(upper)

        print(f"[{self.service}] *** Capacity updated: {self.capacity:.2f} req/s per replica ***")

    def get_capacity(self):
        return self.capacity

    def underload_episode_reset(self):
        self.underload_episode = False
