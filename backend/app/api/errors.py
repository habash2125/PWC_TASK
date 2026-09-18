"""RFC 7807 problem-details errors.

The response never carries SQL, DSNs, stack traces or provider payloads.  The
detail goes to the log under the ``request_id`` that the response *does* carry.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.observability.request_context import request_id_var

log = logging.getLogger("lens.request")

PROBLEM_TYPE_BASE = "https://lens.dev/problems/"


class LensError(Exception):
    """Base for errors that are safe to surface to a client as-is."""

    status: int = 400
    code: str = "bad_request"
    title: str = "Bad request"

    def __init__(self, detail: str | None = None, *, extra: dict[str, Any] | None = None, code: str | None = None):
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.extra = extra or {}
        if code:
            self.code = code


class AuthenticationError(LensError):
    status, code, title = 401, "unauthenticated", "Authentication required"


class ForbiddenError(LensError):
    status, code, title = 403, "forbidden", "Forbidden"


class NotFoundError(LensError):
    status, code, title = 404, "not_found", "Not found"


class ConflictError(LensError):
    status, code, title = 409, "conflict", "Conflict"


class ValidationFailed(LensError):
    status, code, title = 422, "validation_failed", "Validation failed"


class RateLimited(LensError):
    status, code, title = 429, "rate_limited", "Too many requests"


class BudgetExceeded(LensError):
    status, code, title = 429, "budget_exceeded", "Usage budget exceeded"


class GuardBlocked(LensError):
    status, code, title = 422, "guard_blocked", "Request blocked by a safety guard"


class ScopeUnavailable(LensError):
    status, code, title = 503, "scope_unavailable", "Couldn't verify your data permissions"


class ProviderUnavailable(LensError):
    status, code, title = 503, "provider_unavailable", "We couldn't answer that right now"


class PayloadTooLarge(LensError):
    status, code, title = 413, "payload_too_large", "Payload too large"


def problem(
    status: int, title: str, detail: str, code: str, request: Request | None = None, **extra: Any
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE_BASE + code,
        "title": title,
        "status": status,
        "detail": detail,
        "request_id": request_id_var.get(),
    }
    if request is not None:
        body["instance"] = str(request.url.path)
    body.update(extra)
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(status_code=status, content=body, media_type="application/problem+json", headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LensError)
    async def _lens_error(request: Request, exc: LensError) -> JSONResponse:
        if exc.status >= 500:
            log.error("request failed", extra={"code": exc.code, "detail": exc.detail, "path": request.url.path})
        return problem(exc.status, exc.title, exc.detail, exc.code, request, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": [str(p) for p in e.get("loc", [])], "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return problem(
            422,
            "Validation failed",
            "Request body or parameters are invalid",
            "validation_failed",
            request,
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        title = {401: "Authentication required", 403: "Forbidden", 404: "Not found", 405: "Method not allowed"}.get(
            exc.status_code, "Error"
        )
        detail = exc.detail if isinstance(exc.detail, str) else title
        return problem(exc.status_code, title, detail, f"http_{exc.status_code}", request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled exception", extra={"path": request.url.path, "exc_type": type(exc).__name__})
        return problem(
            500, "Internal error", "Something went wrong; quote the request_id when reporting it.", "internal", request
        )
