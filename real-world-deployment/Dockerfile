# Use official Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy autoscaler code
COPY capacity_learner.py .
COPY fanout_learner.py .
COPY panscaler.py .

# Set environment variables defaults (can be overridden in the pod YAML)
ENV SERVICE_NAME=frontend \
    PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 \
    POLL_INTERVAL=10 \
    MIN_INSTANCES=1 \
    PYTHONUNBUFFERED=1

# Run autoscaler
CMD ["python", "-u", "panscaler.py"]
