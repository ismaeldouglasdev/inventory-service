"""Prometheus metrics for Inventory Service.

Usage:
    from app.utils.metrics import metrics

    # Increment a counter
    metrics.requests_total.labels(method="GET", endpoint="/v1/health", status="200").inc()

    # Observe duration
    metrics.request_duration.labels(method="POST", endpoint="/v1/store/sync").observe(0.42)

    # Track adapter calls
    metrics.adapter_requests.labels(channel="mercadolivre", operation="update_stock").inc()
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, generate_latest, REGISTRY

# ── HTTP Request Metrics ──────────────────────────────────────────────────

requests_total = Counter(
    "inventory_requests_total",
    "Total HTTP requests",
    labelnames=["method", "endpoint", "status"],
)

request_duration = Histogram(
    "inventory_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

requests_in_flight = Gauge(
    "inventory_requests_in_flight",
    "Current number of HTTP requests in flight",
    labelnames=["method", "endpoint"],
)

# ── Adapter / Channel Metrics ─────────────────────────────────────────────

adapter_requests = Counter(
    "inventory_adapter_requests_total",
    "Total adapter (channel) requests",
    labelnames=["channel", "operation", "status"],
)

adapter_duration = Histogram(
    "inventory_adapter_duration_seconds",
    "Adapter call duration in seconds",
    labelnames=["channel", "operation"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

adapter_failures = Counter(
    "inventory_adapter_failures_total",
    "Total adapter failures by channel",
    labelnames=["channel", "operation"],
)

adapter_rate_limit_remaining = Gauge(
    "inventory_adapter_rate_limit_remaining",
    "Remaining rate limit per channel",
    labelnames=["channel"],
)

# ── Circuit Breaker Metrics ───────────────────────────────────────────────

circuit_breaker_state = Gauge(
    "inventory_circuit_breaker_state",
    "Circuit breaker state per channel (0=CLOSED, 1=HALF_OPEN, 2=OPEN)",
    labelnames=["channel"],
)

circuit_breaker_failures = Gauge(
    "inventory_circuit_breaker_failures",
    "Current failure count per channel in circuit breaker",
    labelnames=["channel"],
)

# ── Event Store Metrics ───────────────────────────────────────────────────

events_total = Gauge(
    "inventory_events_total",
    "Current event count by state",
    labelnames=["state"],
)

events_processed_total = Counter(
    "inventory_events_processed_total",
    "Total events processed (cumulative)",
    labelnames=["state"],
)

events_dead_total = Counter(
    "inventory_events_dead_total",
    "Total events that reached DEAD state",
)

# ── CDC Agent Metrics ─────────────────────────────────────────────────────

cdc_polls_total = Counter(
    "inventory_cdc_polls_total",
    "Total CDC Agent poll cycles",
)

cdc_changes_detected = Counter(
    "inventory_cdc_changes_detected",
    "Total changes detected by CDC Agent",
)

# ── Database Metrics ──────────────────────────────────────────────────────

db_query_duration = Histogram(
    "inventory_db_query_duration_seconds",
    "Database query duration in seconds",
    labelnames=["query"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

# ── Proxy Pool Metrics ────────────────────────────────────────────────────

proxy_pool_size = Gauge(
    "inventory_proxy_pool_size",
    "Number of proxies in the pool",
    labelnames=["status"],  # healthy, degraded, dead
)

# ── Queue Depth ───────────────────────────────────────────────────────────

queue_depth = Gauge(
    "inventory_queue_depth",
    "Current queue depth for pending/retry events",
    labelnames=["queue"],
)

# ── Agent Communication Metrics ───────────────────────────────────────────

agent_messages_total = Counter(
    "inventory_agent_messages_total",
    "Total agent messages by direction and type",
    labelnames=["direction", "type"],  # direction: sent, received; type: message, response, status, error
)

agent_messages_pending = Gauge(
    "inventory_agent_messages_pending",
    "Current number of pending (unread) messages by target",
    labelnames=["target"],
)

agent_bridge_status = Gauge(
    "inventory_agent_bridge_status",
    "Agent bridge status per agent (1=online, 0=offline)",
    labelnames=["agent"],
)

agent_activity_timestamp = Gauge(
    "inventory_agent_activity_timestamp",
    "Last activity timestamp (unix epoch) per agent",
    labelnames=["agent"],
)


# ── Helper ────────────────────────────────────────────────────────────────

CB_STATE_MAP = {
    "CLOSED": 0,
    "HALF_OPEN": 1,
    "OPEN": 2,
}


def generate_metrics() -> bytes:
    """Generate Prometheus-formatted metrics output."""
    return generate_latest(REGISTRY)
