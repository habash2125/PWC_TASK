"""Auth routes: register (admin only), signup (public, viewer role, no scope), login, refresh (rotating, reuse-detecting), logout, me."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, DbSession, SettingsDep, request_meta, require_role
from app.api.errors import AuthenticationError, ConflictError
from app.api.schemas.auth import (
    LoginRequest,
    MessageOut,
    PrincipalOut,
    RegisterRequest,
    ScopeOut,
    SignupRequest,
    TokenResponse,
    UserOut,
)
from app.core.auth import get_token_service
from app.core.auth.password import hash_password
from app.core.auth.providers import AuthenticationFailed, Credentials, LocalPasswordProvider, principal_from_user
from app.core.auth.rbac import Principal
from app.db.models import Tenant, UserRole
from app.db.repos.audit import AuditRepo
from app.db.repos.users import TokenRepo, UserRepo
from app.observability import metrics

log = logging.getLogger("lens.request")
router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "lens_refresh"
COOKIE_PATH = "/api/v1/auth"


def _set_refresh_cookie(response: Response, token: str, settings) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_ttl_days * 86400,
        path=COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response, settings) -> None:
    response.delete_cookie(
        REFRESH_COOKIE, path=COOKIE_PATH, httponly=True, secure=settings.cookie_secure, samesite="strict"
    )


async def _issue_pair(
    session, settings, principal: Principal, meta: dict, family_id: uuid.UUID | None = None
) -> tuple[str, datetime, str]:
    tokens = get_token_service()
    access, exp = tokens.issue_access(
        user_id=principal.id, tenant_id=principal.tenant_id, role=principal.role.value, email=principal.email
    )
    refresh = tokens.new_refresh_token()
    await TokenRepo(session).issue(
        user_id=principal.id,
        token_hash=tokens.hash_refresh_token(refresh),
        family_id=family_id or uuid.uuid4(),
        expires_at=tokens.refresh_expiry(),
        user_agent=meta.get("user_agent"),
        ip=meta.get("ip"),
    )
    return access, exp, refresh


@router.post("/register", response_model=UserOut, status_code=201)
async def register(
    body: RegisterRequest,
    session: DbSession,
    request: Request,
    principal: Annotated[Principal, Depends(require_role(UserRole.admin))],
) -> UserOut:
    """Admin-only registration: the path that sets a role; data scope is granted separately."""
    users = UserRepo(session)
    if await users.by_email(body.email) is not None:
        raise ConflictError("A user with that e-mail already exists")
    user = await users.create(
        tenant_id=principal.tenant_id,
        email=body.email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        role=body.role,
    )
    await AuditRepo(session).write(
        action="user.register",
        tenant_id=principal.tenant_id,
        actor_user_id=principal.id,
        object_type="app_user",
        object_id=user.id,
        metadata={"role": body.role.value},
        **request_meta(request),
    )
    return UserOut.model_validate(user)


@router.post("/signup", response_model=TokenResponse, status_code=201)
async def signup(
    body: SignupRequest, session: DbSession, settings: SettingsDep, request: Request, response: Response
) -> TokenResponse:
    """Public self-service sign-up.

    New accounts land in the single demo tenant with the viewer role and no access
    scope. Access scope is fail-closed (see access_scope.py): a user with no scope row
    can sign in but sees no analytics rows until an admin grants one via /auth/register
    -equivalent scope management — self-signup never grants data access on its own.
    """
    users = UserRepo(session)
    if await users.by_email(body.email) is not None:
        raise ConflictError("A user with that e-mail already exists")
    tenant = (await session.execute(select(Tenant).where(Tenant.name == settings.seed_tenant_name))).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name=settings.seed_tenant_name)
        session.add(tenant)
        await session.flush()
    user = await users.create(
        tenant_id=tenant.id,
        email=body.email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        role=UserRole.viewer,
    )
    meta = request_meta(request)
    await AuditRepo(session).write(
        action="user.signup",
        tenant_id=tenant.id,
        actor_user_id=user.id,
        object_type="app_user",
        object_id=user.id,
        **meta,
    )
    principal = principal_from_user(user)
    access, exp, refresh = await _issue_pair(session, settings, principal, meta)
    _set_refresh_cookie(response, refresh, settings)
    return TokenResponse(access_token=access, expires_at=exp)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, session: DbSession, settings: SettingsDep, request: Request, response: Response
) -> TokenResponse:
    provider = LocalPasswordProvider(settings, session)
    meta = request_meta(request)
    audit = AuditRepo(session)
    try:
        principal = await provider.authenticate(Credentials(email=body.email, password=body.password))
    except AuthenticationFailed as exc:
        await audit.write(
            action="auth.login_failed", tenant_id=None, actor_user_id=None, metadata={"reason": exc.reason}, **meta
        )
        await session.commit()  # persist the failure count even though we raise
        raise AuthenticationError("Invalid e-mail or password") from None
    access, exp, refresh = await _issue_pair(session, settings, principal, meta)
    await audit.write(
        action="auth.login",
        tenant_id=principal.tenant_id,
        actor_user_id=principal.id,
        object_type="app_user",
        object_id=principal.id,
        **meta,
    )
    _set_refresh_cookie(response, refresh, settings)
    return TokenResponse(access_token=access, expires_at=exp)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    session: DbSession,
    settings: SettingsDep,
    request: Request,
    response: Response,
    lens_refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> TokenResponse:
    if not lens_refresh:
        raise AuthenticationError("No refresh token")
    tokens = get_token_service()
    repo = TokenRepo(session)
    row = await repo.by_hash(tokens.hash_refresh_token(lens_refresh))
    if row is None:
        _clear_refresh_cookie(response, settings)
        raise AuthenticationError("Invalid refresh token")
    now = datetime.now(UTC)
    if row.revoked_at is not None:
        # reuse of a consumed token: the family is compromised, revoke all of it
        revoked = await repo.revoke_family(row.family_id)
        await AuditRepo(session).write(
            action="auth.refresh_reuse_detected",
            tenant_id=None,
            actor_user_id=row.user_id,
            object_type="refresh_token_family",
            object_id=row.family_id,
            metadata={"revoked": revoked},
            **request_meta(request),
        )
        metrics.auth_events.labels("refresh_reuse").inc()
        await session.commit()  # the revocation must survive the 401 we are about to raise
        _clear_refresh_cookie(response, settings)
        raise AuthenticationError("Refresh token reuse detected; please sign in again")
    if row.expires_at <= now:
        _clear_refresh_cookie(response, settings)
        raise AuthenticationError("Refresh token expired")
    user = await UserRepo(session).by_id(row.user_id)
    if user is None or not user.is_active:
        _clear_refresh_cookie(response, settings)
        raise AuthenticationError("Account unavailable")
    from app.core.auth.providers import principal_from_user

    principal = principal_from_user(user)
    meta = request_meta(request)
    access, exp = tokens.issue_access(
        user_id=principal.id, tenant_id=principal.tenant_id, role=principal.role.value, email=principal.email
    )
    new_refresh = tokens.new_refresh_token()
    new_row = await repo.issue(
        user_id=principal.id,
        token_hash=tokens.hash_refresh_token(new_refresh),
        family_id=row.family_id,
        expires_at=tokens.refresh_expiry(),
        user_agent=meta.get("user_agent"),
        ip=meta.get("ip"),
    )
    await repo.rotate(row, new_row)
    metrics.auth_events.labels("refresh_ok").inc()
    _set_refresh_cookie(response, new_refresh, settings)
    return TokenResponse(access_token=access, expires_at=exp)


@router.post("/logout", response_model=MessageOut)
async def logout(
    session: DbSession,
    settings: SettingsDep,
    request: Request,
    response: Response,
    lens_refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> MessageOut:
    if lens_refresh:
        tokens = get_token_service()
        repo = TokenRepo(session)
        row = await repo.by_hash(tokens.hash_refresh_token(lens_refresh))
        if row is not None:
            await repo.revoke_family(row.family_id)
            await AuditRepo(session).write(
                action="auth.logout",
                tenant_id=None,
                actor_user_id=row.user_id,
                object_type="refresh_token_family",
                object_id=row.family_id,
                **request_meta(request),
            )
    _clear_refresh_cookie(response, settings)
    return MessageOut(message="signed out")


@router.get("/me", response_model=PrincipalOut)
async def me(principal: CurrentPrincipal, session: DbSession) -> PrincipalOut:
    scopes = await UserRepo(session).scopes_for(principal.id)
    return PrincipalOut(
        id=principal.id,
        email=principal.email,
        full_name=principal.full_name,
        role=principal.role,
        tenant_id=principal.tenant_id,
        scopes=[
            ScopeOut(data_source_id=s.data_source_id, scope_key=s.scope_key, scope_values=list(s.scope_values))
            for s in scopes
        ],
    )
