"""HTTP middleware: correlation ids, security headers, request size cap, per-IP rate limit."""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.api.errors import problem
from app.config import Settings
from app.observability import metrics
from app.observability.request_context import (
    LlmCallCounter,
    llm_counter_var,
    new_id,
    request_id_var,
    session_id_var,
    trace_id_var,
    turn_id_var,
    user_id_var,
)
from app.observability.tracing import root_span, set_attributes

log = logging.getLogger("lens.request")

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Populates ContextVars, times the request, sets security headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = new_id()
        trace_id = new_id(16)
        tokens = [
            request_id_var.set(request_id),
            trace_id_var.set(trace_id),
            user_id_var.set(None),
            session_id_var.set(None),
            turn_id_var.set(None),
            llm_counter_var.set(LlmCallCounter()),
        ]
        started = time.perf_counter()
        route = "unmatched"
        traced = not request.url.path.endswith(("/health", "/health/ready", "/metrics"))
        try:
            if traced:
                with root_span(f"http {request.method} {request.url.path}", http_method=request.method) as span:
                    trace_id = trace_id_var.get() or trace_id
                    response = await call_next(request)
                    set_attributes(span, http_status=response.status_code)
            else:
                response = await call_next(request)
        except Exception:  # pragma: no cover - handled by exception handlers normally
            log.exception("middleware caught unhandled error")
            response = problem(500, "Internal error", "Something went wrong", "internal", request)
        finally:
            elapsed = time.perf_counter() - started
            route_obj = request.scope.get("route")
            if route_obj is not None:
                route = getattr(route_obj, "path", route)
        status = response.status_code
        metrics.http_requests.labels(request.method, route, str(status)).inc()
        metrics.http_latency.labels(request.method, route).observe(elapsed)
        if not request.url.path.endswith(("/health", "/metrics")):
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "route": route,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 1),
                    "ip": client_ip(request),
                },
            )
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Trace-Id"] = trace_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        for var, token in zip(
            (request_id_var, trace_id_var, user_id_var, session_id_var, turn_id_var, llm_counter_var),
            tokens,
            strict=True,
        ):
            var.reset(token)
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_bytes: int):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            return problem(
                413, "Payload too large", f"Request body exceeds {self.max_bytes} bytes", "payload_too_large", request
            )
        return await call_next(request)


class IpRateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-IP limit backed by Redis.  Fails open (logged, counted) if Redis is down."""

    def __init__(self, app, settings: Settings, redis_getter):
        super().__init__(app)
        self.limit = settings.rate_limit_ip_per_minute
        self.redis_getter = redis_getter

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.endswith(("/health", "/health/ready", "/metrics")):
            return await call_next(request)
        redis = self.redis_getter()
        if redis is not None:
            key = f"rl:ip:{client_ip(request)}:{int(time.time() // 60)}"
            try:
                count = await redis.incr(key)
                if count == 1:
                    await redis.expire(key, 65)
                if count > self.limit:
                    metrics.rate_limited.labels("ip").inc()
                    resp = problem(429, "Too many requests", "Per-IP rate limit exceeded", "rate_limited", request)
                    resp.headers["Retry-After"] = "60"
                    return resp
            except Exception as exc:  # fail open for availability; the per-user limiter still applies
                log.warning("ip rate limiter unavailable", extra={"error": type(exc).__name__})
        return await call_next(request)
