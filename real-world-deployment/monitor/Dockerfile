FROM python:3.12-slim

WORKDIR /app

COPY monitor.py .
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Defaults (override in YAML)
ENV POLL_INTERVAL=45
ENV PROMETHEUS_TARGET=http://prometheus.monitoring.svc.cluster.local:9090
ENV NAMESPACE=default
ENV ONLINE_BOUTIQUE_DEPLOYMENTS=frontend|cartservice|productcatalogservice|currencyservice|paymentservice|shippingservice|emailservice|checkoutservice|recommendationservice|adservice
ENV WORKLOAD_DELTA_THRESHOLD=0.3
ENV WINDOW_SIZE=10
ENV PYTHONUNBUFFERED=1

CMD ["python", "monitor.py"]
