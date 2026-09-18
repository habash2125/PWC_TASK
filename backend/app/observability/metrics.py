"""Prometheus metrics.  Exposed at ``/api/v1/metrics`` in text format."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

registry = CollectorRegistry(auto_describe=True)

_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120)

http_requests = Counter("lens_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=registry)
http_latency = Histogram(
    "lens_http_request_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
    registry=registry,
)

stage_latency = Histogram(
    "lens_stage_seconds", "Pipeline stage latency", ["stage"], buckets=_LATENCY_BUCKETS, registry=registry
)
turn_tokens = Histogram(
    "lens_turn_tokens",
    "Tokens per turn",
    ["direction"],
    buckets=(100, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000),
    registry=registry,
)
turn_cost_usd = Histogram(
    "lens_turn_cost_usd",
    "Cost per turn in USD",
    buckets=(0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),
    registry=registry,
)
turn_status = Counter("lens_turns_total", "Turns by terminal status", ["status"], registry=registry)

llm_calls = Counter("lens_llm_calls_total", "Model calls", ["stage", "model", "outcome"], registry=registry)
llm_retries = Counter("lens_llm_retries_total", "Model call retries", ["model"], registry=registry)
llm_fallbacks = Counter(
    "lens_llm_fallbacks_total", "Model fallbacks taken", ["from_model", "to_model"], registry=registry
)
llm_circuit_open = Gauge("lens_llm_circuit_open", "1 when the provider circuit is open", ["model"], registry=registry)

guard_blocks = Counter(
    "lens_guard_blocks_total", "Guard blocks by kind and reason", ["kind", "reason"], registry=registry
)
guard_shadow_disagreements = Counter(
    "lens_guard_shadow_disagreements_total",
    "Parser shadow verdict disagreed with the enforcer",
    ["direction"],
    registry=registry,
)
sql_executions = Counter("lens_sql_executions_total", "SQL executions", ["outcome"], registry=registry)
sql_latency = Histogram("lens_sql_seconds", "SQL execution latency", buckets=_LATENCY_BUCKETS, registry=registry)
chart_capture_failures = Counter(
    "lens_chart_capture_failures_total", "Chart capture failures", ["reason"], registry=registry
)
python_exec = Counter("lens_python_exec_total", "Sandboxed python executions", ["outcome"], registry=registry)

tile_refresh_latency = Histogram(
    "lens_tile_refresh_seconds", "Tile refresh latency", buckets=_LATENCY_BUCKETS, registry=registry
)
tile_refresh_status = Counter("lens_tile_refresh_total", "Tile refreshes", ["status"], registry=registry)

active_users = Gauge("lens_active_users", "Users with a request in the last 5 minutes", registry=registry)
rate_limited = Counter("lens_rate_limited_total", "Requests rejected by rate limiting", ["bucket"], registry=registry)
auth_events = Counter("lens_auth_events_total", "Auth events", ["event"], registry=registry)


def render() -> bytes:
    return generate_latest(registry)
