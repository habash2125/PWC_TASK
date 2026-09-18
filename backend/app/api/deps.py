"""FastAPI dependencies: sessions, principals, RBAC, per-dashboard grants, rate limits, idempotency.

Authorisation lives here, never in route bodies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, Response

from app.api.errors import AuthenticationError, ForbiddenError, NotFoundError, RateLimited
from app.api.middleware import client_ip
from app.config import Settings
from app.core.auth import get_token_service
from app.core.auth.providers import principal_from_user
from app.core.auth.rbac import Principal, dashboard_role_at_least, effective_dashboard_role, user_role_at_least
from app.core.auth.tokens import TokenError
from app.core.cache import get_redis
from app.db.models import DashboardRole, UserRole
from app.db.repos.users import UserRepo
from app.db.session import get_session
from app.observability import metrics
from app.observability.request_context import tenant_id_var, user_id_var

log = logging.getLogger("lens.request")

DbSession = Annotated[AsyncSession, Depends(get_session)]


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


async def current_principal(
    request: Request,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = get_token_service().verify_access(token)
    except TokenError as exc:
        log.info("token rejected", extra={"reason": str(exc)})
        raise AuthenticationError("Invalid or expired token") from None
    user = await UserRepo(session).by_id(claims.user_id)
    if user is None or not user.is_active or user.tenant_id != claims.tenant_id:
        raise AuthenticationError("Invalid or expired token")
    user_id_var.set(str(user.id))
    tenant_id_var.set(str(user.tenant_id))
    await _touch_active(str(user.id))
    return principal_from_user(user)


async def _touch_active(user_id: str) -> None:
    """Feeds the active-users gauge: a Redis set per five-minute window."""
    redis = get_redis()
    if redis is None:
        return
    try:
        key = f"active:{int(time.time() // 300)}"
        await redis.sadd(key, user_id)
        await redis.expire(key, 600)
    except Exception:
        pass  # role comes from the row, not the token: a demotion takes effect immediately


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_role(minimum: UserRole) -> Callable[..., Awaitable[Principal]]:
    async def _dep(principal: CurrentPrincipal) -> Principal:
        if not user_role_at_least(principal.role, minimum):
            raise ForbiddenError(f"Requires the {minimum.value} role")
        return principal

    return _dep


# ── per-dashboard grants ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DashboardContext:
    dashboard_id: uuid.UUID
    principal: Principal
    effective_role: DashboardRole
    owner_id: uuid.UUID


def require_dashboard_role(minimum: DashboardRole) -> Callable[..., Awaitable[DashboardContext]]:
    """Resolves ``effective_role = min(ceiling(global_role), grant)``.

    A caller without any grant gets **404**, not 403: the existence of objects
    the caller cannot see is never confirmed.
    """

    async def _dep(dashboard_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> DashboardContext:
        from app.db.repos.dashboards import DashboardRepo

        repo = DashboardRepo(session)
        dashboard = await repo.get(dashboard_id, tenant_id=principal.tenant_id)
        if dashboard is None:
            raise NotFoundError("Dashboard not found")
        grant = await repo.grant_for(dashboard_id, principal.id)
        if grant is None and dashboard.visibility.value == "tenant":
            grant = DashboardRole.viewer  # implicit tenant-wide viewer grant
        effective = effective_dashboard_role(principal.role, grant)
        if effective is None:
            raise NotFoundError("Dashboard not found")
        if not dashboard_role_at_least(effective, minimum):
            raise ForbiddenError(f"Requires {minimum.value} access to this dashboard")
        return DashboardContext(
            dashboard_id=dashboard_id, principal=principal, effective_role=effective, owner_id=dashboard.owner_id
        )

    return _dep


# ── per-user rate limiting ───────────────────────────────────────────────────


def rate_limit(bucket: str) -> Callable[..., Awaitable[None]]:
    async def _dep(request: Request, principal: CurrentPrincipal, settings: SettingsDep) -> None:
        redis = get_redis()
        if redis is None:
            return
        limit = settings.rate_limit_chat_per_minute if bucket == "chat" else settings.rate_limit_per_minute
        key = f"rl:user:{bucket}:{principal.id}:{int(time.time() // 60)}"
        try:
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, 65)
        except Exception as exc:  # availability over strictness; the IP limiter and budgets still apply
            log.warning("user rate limiter unavailable", extra={"error": type(exc).__name__})
            return
        if count > limit:
            metrics.rate_limited.labels(bucket).inc()
            raise RateLimited(f"Rate limit for {bucket} exceeded; try again in a minute", extra={"retry_after": 60})

    return _dep


# ── idempotency keys ─────────────────────────────────────────────────────────


class Idempotency:
    """Replays a stored response for a repeated ``Idempotency-Key`` from the same user (24h)."""

    TTL = 24 * 3600

    def __init__(self, principal: Principal, key: str | None, request: Request):
        self.principal = principal
        self.key = key
        self.request = request

    def _redis_key(self) -> str:
        scope = f"{self.principal.id}:{self.request.method}:{self.request.url.path}:{self.key}"
        return "idem:" + hashlib.sha256(scope.encode()).hexdigest()

    async def replay(self) -> Response | None:
        if not self.key:
            return None
        redis = get_redis()
        if redis is None:
            return None
        try:
            stored = await redis.get(self._redis_key())
        except Exception:
            return None
        if not stored:
            return None
        payload = json.loads(stored)
        return JSONResponse(
            status_code=payload["status"], content=payload["body"], headers={"Idempotent-Replayed": "true"}
        )

    async def store(self, status: int, body: Any) -> None:
        if not self.key:
            return
        redis = get_redis()
        if redis is None:
            return
        try:
            await redis.set(self._redis_key(), json.dumps({"status": status, "body": body}, default=str), ex=self.TTL)
        except Exception:
            pass


async def idempotency(
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> Idempotency:
    return Idempotency(principal, idempotency_key, request)


IdempotencyDep = Annotated[Idempotency, Depends(idempotency)]


def request_meta(request: Request) -> dict[str, str | None]:
    return {"ip": client_ip(request), "user_agent": request.headers.get("user-agent")}
