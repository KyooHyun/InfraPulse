from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

http_requests_total = Counter(
    "http_requests_total",
    "Total number of HTTP requests received by the API",
)

transaction_total = Counter(
    "transaction_total",
    "Total number of transaction transfer requests",
)

transaction_failed_total = Counter(
    "transaction_failed_total",
    "Total number of failed transactions",
)

login_failed_total = Counter(
    "login_failed_total",
    "Total number of failed login attempts",
)

anomaly_event_total = Counter(
    "anomaly_event_total",
    "Total number of anomaly detection events",
)

anomaly_transaction_failure_total = Counter(
    "anomaly_transaction_failure_total",
    "Total number of transaction failure anomaly events",
)

anomaly_latency_total = Counter(
    "anomaly_latency_total",
    "Total number of API latency anomaly events",
)

anomaly_login_failure_total = Counter(
    "anomaly_login_failure_total",
    "Total number of repeated login failure anomaly events",
)

anomaly_high_value_total = Counter(
    "anomaly_high_value_total",
    "Total number of high-value transaction anomaly events",
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
)


def metrics_response():
    return generate_latest(), CONTENT_TYPE_LATEST
