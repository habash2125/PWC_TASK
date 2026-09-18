"""Idempotent app-db seed: tenant, four users, the analytics data source with its allow-list, access scopes.

Run with ``python -m app.seed``.  Running it twice is a no-op apart from refreshing view metadata.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.auth.password import hash_password
from app.db.models import AccessScope, AppUser, DataSource, DataSourceView, Tenant, UserRole
from app.db.seed.analytics_catalog import VIEWS
from app.db.session import dispose_app_db, init_app_db, session_factory

# email, role, scope over client_id, settings attribute holding the password
SEED_USERS = [
    ("admin@lens.demo", "Lens Admin", UserRole.admin, ["*"], "seed_admin_password"),
    ("analyst@lens.demo", "Ava Analyst", UserRole.analyst, ["*"], "seed_analyst_password"),
    ("analyst2@lens.demo", "Ben Analyst", UserRole.analyst, [1, 2, 3], "seed_analyst2_password"),
    ("partner@lens.demo", "Priya Partner", UserRole.viewer, [1, 2], "seed_partner_password"),
]
DATA_SOURCE_NAME = "Delivery Portfolio"


async def _get_or_create_tenant(session: AsyncSession, name: str) -> Tenant:
    tenant = (await session.execute(select(Tenant).where(Tenant.name == name))).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name=name)
        session.add(tenant)
        await session.flush()
    return tenant


async def _upsert_user(
    session: AsyncSession, tenant: Tenant, email: str, name: str, role: UserRole, password: str
) -> AppUser:
    user = (
        await session.execute(select(AppUser).where(AppUser.tenant_id == tenant.id, AppUser.email == email))
    ).scalar_one_or_none()
    if user is None:
        user = AppUser(
            tenant_id=tenant.id, email=email, full_name=name, role=role, password_hash=hash_password(password)
        )
        session.add(user)
        await session.flush()
    return user


async def _upsert_source(session: AsyncSession, settings: Settings, tenant: Tenant, admin: AppUser) -> DataSource:
    source = (
        await session.execute(
            select(DataSource).where(DataSource.tenant_id == tenant.id, DataSource.name == DATA_SOURCE_NAME)
        )
    ).scalar_one_or_none()
    if source is None:
        source = DataSource(
            tenant_id=tenant.id,
            name=DATA_SOURCE_NAME,
            dsn_secret_ref=settings.analytics_dsn_secret_ref,
            read_only_role=settings.analytics_db_readonly_user,
            dialect="postgresql",
            created_by=admin.id,
        )
        session.add(source)
        await session.flush()
    existing = {
        v.view_name: v
        for v in (
            await session.execute(select(DataSourceView).where(DataSourceView.data_source_id == source.id))
        ).scalars()
    }
    for v in VIEWS:
        row = existing.get(v.name)
        if row is None:
            row = DataSourceView(data_source_id=source.id, view_name=v.name, description=v.description)
            session.add(row)
        # the catalogue is the source of truth for metadata: every run refreshes it (admin edits made through the
        # API are runtime curation and are expected to be re-applied after a reseed)
        row.description = v.description
        row.business_rules = v.business_rules
        row.column_metadata = [c.as_dict() for c in v.columns]
        row.scope_column = v.scope_column
        row.allow_row_samples = v.allow_row_samples
    return source


async def _upsert_scope(session: AsyncSession, user: AppUser, source: DataSource, values: list) -> None:
    scope = (
        await session.execute(
            select(AccessScope).where(
                AccessScope.user_id == user.id,
                AccessScope.data_source_id == source.id,
                AccessScope.scope_key == "client_id",
            )
        )
    ).scalar_one_or_none()
    if scope is None:
        session.add(AccessScope(user_id=user.id, data_source_id=source.id, scope_key="client_id", scope_values=values))
    else:
        scope.scope_values = values


async def seed(settings: Settings | None = None) -> dict[str, uuid.UUID]:
    settings = settings or get_settings()
    init_app_db(settings)
    ids: dict[str, uuid.UUID] = {}
    try:
        async with session_factory()() as session:
            tenant = await _get_or_create_tenant(session, settings.seed_tenant_name)
            users: dict[str, AppUser] = {}
            for email, name, role, _values, pw_attr in SEED_USERS:
                users[email] = await _upsert_user(
                    session, tenant, email, name, role, getattr(settings, pw_attr).get_secret_value()
                )
            source = await _upsert_source(session, settings, tenant, users["admin@lens.demo"])
            for email, _name, _role, values, _pw in SEED_USERS:
                await _upsert_scope(session, users[email], source, values)
            await session.commit()
            ids["tenant"] = tenant.id
            ids["data_source"] = source.id
            ids.update({email: u.id for email, u in users.items()})
        print(
            f"app seed: tenant '{settings.seed_tenant_name}', {len(SEED_USERS)} users, source '{DATA_SOURCE_NAME}' with {len(VIEWS)} views",
            flush=True,
        )
    finally:
        await dispose_app_db()
    return ids


if __name__ == "__main__":
    asyncio.run(seed())
