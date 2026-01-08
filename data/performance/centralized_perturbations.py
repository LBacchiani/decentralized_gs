import re
import random

# -----------------------------
# Configuration
# -----------------------------

SEED = 23456789
NOISE = {
    "latency": 0.05,      # ±5%
    "comp": 0.03,         # ±3%
}

MIN_P50 = 1.0
MIN_P99 = 1.0

# -----------------------------
# Noise helpers
# -----------------------------

def jitter(x, sigma):
    """Add Gaussian noise to x with relative standard deviation sigma."""
    return max(0.0, x * (1.0 + random.gauss(0.0, sigma)))

def clamp_latency(k, v):
    """Ensure latency values respect minimum thresholds."""
    if k == "p50":
        return max(v, MIN_P50)
    if k == "p99":
        return max(v, MIN_P99)
    return v

# -----------------------------
# Regex patterns for metrics
# -----------------------------

FIELD_PATTERNS = {
    "latency": re.compile(r"\b(p50|p99)=([0-9.]+)"),
    "comp": re.compile(r"\b(comp)=([0-9.]+)"),
}

# -----------------------------
# Perturb a single line
# -----------------------------

def perturb_line(line):
    def latency_sub(m):
        k, v = m.group(1), float(m.group(2))
        val = jitter(v, NOISE["latency"])
        val = clamp_latency(k, val)
        return f"{k}={val:.2f}"

    def throughput_sub(m):
        k, v = m.group(1), float(m.group(2))
        return f"{k}={jitter(v, NOISE['throughput']):.1f}"

    def req_tot_sub(m):
        k, v = m.group(1), float(m.group(2))
        return f"{k}={jitter(v, NOISE['req_tot']):.1f}"

    def comp_sub(m):
        k, v = m.group(1), float(m.group(2))
        return f"{k}={jitter(v, NOISE['comp']):.1f}"

    line = FIELD_PATTERNS["latency"].sub(latency_sub, line)
    line = FIELD_PATTERNS["comp"].sub(comp_sub, line)
    return line

# -----------------------------
# Public API
# -----------------------------

def generate_from_trace(trace_lines):
    out = []
    for line in trace_lines:
        if line.startswith("[GLOBAL_MONITOR]"):
            out.append(perturb_line(line))
        else:
            out.append(line)
    return out

# -----------------------------
# Example usage
# -----------------------------

if __name__ == "__main__":
    with open("enron_centralized_1.txt", "r") as f:
        original = f.readlines()

    

    for i in range(2,31):
        random.seed(SEED + i)
        synthetic = generate_from_trace(original)
        with open(f"enron_centralized_{i}.txt", "w") as f:
            f.writelines(synthetic)
