from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccessScope, AppUser, RefreshToken, UserRole


class UserRepo:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def by_id(self, user_id: uuid.UUID) -> AppUser | None:
        return await self.s.get(AppUser, user_id)

    async def by_email(self, email: str) -> AppUser | None:
        # emails are unique per tenant; the local provider has no tenant hint at login, so the
        # seeded single-tenant deployment resolves by e-mail alone (documented in ARCHITECTURE.md)
        return (await self.s.execute(select(AppUser).where(AppUser.email == email))).scalars().first()

    async def list_in_tenant(self, tenant_id: uuid.UUID) -> list[AppUser]:
        return list(
            (
                await self.s.execute(select(AppUser).where(AppUser.tenant_id == tenant_id).order_by(AppUser.email))
            ).scalars()
        )

    async def create(
        self, *, tenant_id: uuid.UUID, email: str, password_hash: str | None, full_name: str | None, role: UserRole
    ) -> AppUser:
        user = AppUser(tenant_id=tenant_id, email=email, password_hash=password_hash, full_name=full_name, role=role)
        self.s.add(user)
        await self.s.flush()
        return user

    async def record_failed_login(self, user: AppUser, *, max_failures: int, base_seconds: int) -> None:
        user.failed_login_count += 1
        if user.failed_login_count >= max_failures:
            # exponential cooldown: base * 2^(failures - max), capped at 1 hour
            exponent = min(user.failed_login_count - max_failures, 7)
            user.locked_until = datetime.now(UTC) + timedelta(seconds=min(3600, base_seconds * (2**exponent)))
        await self.s.flush()

    async def record_successful_login(self, user: AppUser) -> None:
        user.failed_login_count = 0
        user.locked_until = None
        await self.s.flush()

    async def scopes_for(self, user_id: uuid.UUID, data_source_id: uuid.UUID | None = None) -> list[AccessScope]:
        q = select(AccessScope).where(AccessScope.user_id == user_id)
        if data_source_id is not None:
            q = q.where(AccessScope.data_source_id == data_source_id)
        return list((await self.s.execute(q)).scalars())

    async def set_scope(
        self, user_id: uuid.UUID, data_source_id: uuid.UUID, scope_key: str, values: list
    ) -> AccessScope:
        row = (
            await self.s.execute(
                select(AccessScope).where(
                    AccessScope.user_id == user_id,
                    AccessScope.data_source_id == data_source_id,
                    AccessScope.scope_key == scope_key,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = AccessScope(user_id=user_id, data_source_id=data_source_id, scope_key=scope_key, scope_values=values)
            self.s.add(row)
        else:
            row.scope_values = values
        await self.s.flush()
        return row


class TokenRepo:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def issue(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        family_id: uuid.UUID,
        expires_at: datetime,
        user_agent: str | None,
        ip: str | None,
    ) -> RefreshToken:
        row = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            family_id=family_id,
            expires_at=expires_at,
            user_agent=user_agent,
            ip=ip,
        )
        self.s.add(row)
        await self.s.flush()
        return row

    async def by_hash(self, token_hash: str) -> RefreshToken | None:
        return (
            await self.s.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        ).scalar_one_or_none()

    async def revoke_family(self, family_id: uuid.UUID) -> int:
        result = await self.s.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        return result.rowcount or 0

    async def rotate(self, old: RefreshToken, new: RefreshToken) -> None:
        old.revoked_at = datetime.now(UTC)
        old.replaced_by_id = new.id
        await self.s.flush()

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        result = await self.s.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        return result.rowcount or 0
