"""Shared fixtures.

Tests run against the real app-db and analytics-db (started with
``docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d app-db analytics-db cache``)
and the real guard, sandbox and auth code.  Only the model provider is scripted.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("GUARD_ENFORCER", "parser")
os.environ.setdefault("RATE_LIMIT_CHAT_PER_MINUTE", "1000")
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("RATE_LIMIT_IP_PER_MINUTE", "100000")
os.environ.setdefault("OTEL_BATCH_DELAY_MS", "50")

from app.config import Settings, get_settings  # noqa: E402
from app.core.auth.password import hash_password  # noqa: E402
from app.db.models import AccessScope, AppUser, DataSource, Tenant, UserRole  # noqa: E402

RESET_TABLES = (
    "tile_refresh",
    "guard_event",
    "feedback",
    "dashboard_grant",
    "dashboard_tile",
    "dashboard_group",
    "dashboard",
    "saved_chart",
    "turn_chart",
    "turn",
    "chat_session",
    "refresh_token",
    "usage_counter",
    "audit_log",
    "trace_span",
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session")
async def app(settings: Settings):
    from app.main import create_app

    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture(scope="session")
async def seeded(app) -> dict[str, Any]:
    """Makes sure the seeded tenant/users/source exist and returns their ids."""
    from sqlalchemy import select

    from app.db.seed.app_seed import DATA_SOURCE_NAME
    from app.db.session import session_factory

    async with session_factory()() as session:
        tenant = (await session.execute(select(Tenant))).scalars().first()
        assert tenant is not None, "run `python -m app.seed` first"
        source = (await session.execute(select(DataSource).where(DataSource.name == DATA_SOURCE_NAME))).scalar_one()
        users = {
            u.email: u for u in (await session.execute(select(AppUser).where(AppUser.tenant_id == tenant.id))).scalars()
        }
    return {"tenant_id": tenant.id, "source_id": source.id, "users": {k: v.id for k, v in users.items()}}


@pytest.fixture(autouse=True)
async def clean_db(app):
    """Truncate everything that tests create; seeded rows (tenant, users, source, scopes) survive."""
    from app.db.session import get_engine

    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(RESET_TABLES)} CASCADE"))
        await conn.execute(text("UPDATE app_user SET failed_login_count = 0, locked_until = NULL"))
    yield


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


class Api:
    """Thin helper around the HTTP client with a bearer token."""

    def __init__(self, client: httpx.AsyncClient, prefix: str = "/api/v1"):
        self.client = client
        self.prefix = prefix
        self.token: str | None = None

    async def login(self, email: str, password: str) -> httpx.Response:
        r = await self.client.post(f"{self.prefix}/auth/login", json={"email": email, "password": password})
        if r.status_code == 200:
            self.token = r.json()["access_token"]
        return r

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        h.update(extra or {})
        return h

    async def get(self, path: str, **kw):
        return await self.client.get(self.prefix + path, headers=self._headers(kw.pop("headers", None)), **kw)

    async def post(self, path: str, **kw):
        return await self.client.post(self.prefix + path, headers=self._headers(kw.pop("headers", None)), **kw)

    async def put(self, path: str, **kw):
        return await self.client.put(self.prefix + path, headers=self._headers(kw.pop("headers", None)), **kw)

    async def patch(self, path: str, **kw):
        return await self.client.patch(self.prefix + path, headers=self._headers(kw.pop("headers", None)), **kw)

    async def delete(self, path: str, **kw):
        return await self.client.delete(self.prefix + path, headers=self._headers(kw.pop("headers", None)), **kw)


@pytest.fixture
def api(client: httpx.AsyncClient) -> Api:
    return Api(client)


@pytest.fixture
def api_factory(client: httpx.AsyncClient):
    def make() -> Api:
        return Api(client)

    return make


SEED_PASSWORDS = {
    "admin@lens.demo": "seed_admin_password",
    "analyst@lens.demo": "seed_analyst_password",
    "analyst2@lens.demo": "seed_analyst2_password",
    "partner@lens.demo": "seed_partner_password",
}


def password_for(settings: Settings, email: str) -> str:
    return getattr(settings, SEED_PASSWORDS[email]).get_secret_value()


@pytest.fixture
async def login(api_factory, settings: Settings, seeded):
    async def _login(email: str) -> Api:
        a = api_factory()
        r = await a.login(email, password_for(settings, email))
        assert r.status_code == 200, r.text
        return a

    return _login


@pytest.fixture
async def make_user(app, seeded):
    """Creates an ad-hoc user in the seeded tenant with a scope on the seeded source."""
    from app.db.session import session_factory

    async def _make(
        role: UserRole = UserRole.analyst, scope: list | None = None, password: str = "Test!Pass123"
    ) -> tuple[uuid.UUID, str, str]:
        email = f"user-{uuid.uuid4().hex[:8]}@test.lens"
        async with session_factory()() as session:
            user = AppUser(
                tenant_id=seeded["tenant_id"],
                email=email,
                full_name="Test User",
                role=role,
                password_hash=hash_password(password),
            )
            session.add(user)
            await session.flush()
            if scope is not None:
                session.add(
                    AccessScope(
                        user_id=user.id, data_source_id=seeded["source_id"], scope_key="client_id", scope_values=scope
                    )
                )
            await session.commit()
            return user.id, email, password

    return _make
