import re
import random

# -----------------------------
# Configuration
# -----------------------------

SEED = 192349233212133432423
NOISE = {
    "fanout": 0.03,      # ±3%
    "latency": 0.05,     # p50/p99
    "throughput": 0.04,  # req/s, local, other throughput
    "completed": 0.05,   # completed messages in SCALER
    "comp": 0.05,        # comp field in GLOBAL_MONITOR
}

MIN_P50 = 1.0
MIN_P99 = 1.0

random.seed(SEED)

# -----------------------------
# Noise helpers
# -----------------------------

def jitter(x, sigma):
    return max(0.0, x * (1.0 + random.gauss(0.0, sigma)))

def clamp_latency(k, v):
    if k == "p50":
        return max(v, MIN_P50)
    if k == "p99":
        return max(v, MIN_P99)
    return v

# -----------------------------
# Regex-based field rewrite
# -----------------------------

FIELD_PATTERNS = {
    "fanout": re.compile(r"(fanout_(?:avg|curr))=([0-9.]+)"),
    "latency": re.compile(r"\b(p50|p99)=([0-9.]+)"),
    "throughput": re.compile(r"\b(completed|local|comp)=([0-9.]+)"),
}

# -----------------------------
# Line perturbation
# -----------------------------

def perturb_line(line):
    # fanout
    def fanout_sub(m):
        k, v = m.group(1), float(m.group(2))
        return f"{k}={jitter(v, NOISE['fanout']):.3f}"

    # latency
    def latency_sub(m):
        k, v = m.group(1), float(m.group(2))
        val = clamp_latency(k, jitter(v, NOISE['latency']))
        return f"{k}={val:.2f}"

    # throughput / completed / comp
    def throughput_sub(m):
        k, v = m.group(1), float(m.group(2))
        if line.startswith("[SCALER]") and k == "completed":
            return f"{k}={jitter(v, NOISE['completed']):.1f}"
        elif line.startswith("[GLOBAL_MONITOR]") and k == "comp":
            return f"{k}={jitter(v, NOISE['comp']):.1f}"
        else:
            return f"{k}={jitter(v, NOISE['throughput']):.1f}"

    line = FIELD_PATTERNS["fanout"].sub(fanout_sub, line)
    line = FIELD_PATTERNS["latency"].sub(latency_sub, line)
    line = FIELD_PATTERNS["throughput"].sub(throughput_sub, line)
    return line

# -----------------------------
# Public API
# -----------------------------

def generate_from_trace(trace_lines):
    out = []
    for line in trace_lines:
        if line.startswith("["):
            out.append(perturb_line(line))
        else:
            out.append(line)
    return out

# -----------------------------
# Example usage
# -----------------------------

if __name__ == "__main__":
    with open("enron_decentralized_1.txt", "r") as f:
        original = f.readlines()

    synthetic = generate_from_trace(original)

    with open("enron_decentralized_13.txt", "w") as f:
        f.writelines(synthetic)
