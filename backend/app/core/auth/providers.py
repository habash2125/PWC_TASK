"""Authentication providers.

``AuthProvider`` is the seam for enterprise identity: the local password
provider is one implementation; an OIDC provider (Authorization Code + PKCE
against Entra ID / Okta / Ping, JWKS validation, group→role mapping) is the
production path described in ARCHITECTURE.md and slots in behind the same
interface without touching the routers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.auth.password import hash_password, needs_rehash, verify_password
from app.core.auth.rbac import Principal
from app.db.models import AppUser
from app.db.repos.users import UserRepo
from app.observability import metrics


@dataclass(frozen=True, slots=True)
class Credentials:
    email: str
    password: str


class AuthenticationFailed(Exception):
    """Deliberately carries no detail: the login response is constant-shape."""

    def __init__(self, reason: str):
        super().__init__("authentication failed")
        self.reason = reason  # for the audit log only


class AuthProvider(Protocol):
    async def authenticate(self, credentials: Credentials) -> Principal: ...


class LocalPasswordProvider:
    """Argon2id passwords, lockout with exponential cooldown, constant-shape failures."""

    def __init__(self, settings: Settings, session: AsyncSession):
        self.settings = settings
        self.session = session
        self.users = UserRepo(session)

    async def authenticate(self, credentials: Credentials) -> Principal:
        user = await self.users.by_email(credentials.email)
        if user is None:
            # run the hash anyway so timing does not reveal whether the e-mail exists
            verify_password(credentials.password, None)
            metrics.auth_events.labels("login_unknown_user").inc()
            raise AuthenticationFailed("unknown_user")
        if not user.is_active:
            verify_password(credentials.password, None)
            raise AuthenticationFailed("inactive")
        now = datetime.now(UTC)
        if user.locked_until is not None and user.locked_until > now:
            verify_password(credentials.password, None)
            metrics.auth_events.labels("login_locked").inc()
            raise AuthenticationFailed("locked")
        if not verify_password(credentials.password, user.password_hash):
            await self.users.record_failed_login(
                user,
                max_failures=self.settings.login_max_failures,
                base_seconds=self.settings.login_lockout_base_seconds,
            )
            metrics.auth_events.labels("login_bad_password").inc()
            raise AuthenticationFailed("bad_password")
        if user.password_hash and needs_rehash(user.password_hash):
            user.password_hash = hash_password(credentials.password)
        await self.users.record_successful_login(user)
        metrics.auth_events.labels("login_ok").inc()
        return principal_from_user(user)


def principal_from_user(user: AppUser, scopes: dict[str, list] | None = None) -> Principal:
    return Principal(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        role=user.role,
        full_name=user.full_name,
        scopes=scopes or {},
    )
