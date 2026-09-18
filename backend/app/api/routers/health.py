"""Liveness and readiness probes (no auth)."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from starlette.responses import JSONResponse

from app.core.cache import cache_ping
from app.core.llm.client import get_llm
from app.db import analytics_pool
from app.db.session import get_engine

router = APIRouter(tags=["ops"])

_provider_probe_cache: dict[str, object] = {"at": 0.0, "ok": False}


class Health(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str


class Readiness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    app_db: bool
    analytics_db: bool
    cache: bool
    provider: bool
    provider_configured: bool


@router.get("/health", response_model=Health)
async def liveness() -> Health:
    return Health(status="ok")


async def _check(coro, limit: float = 3.0) -> bool:
    try:
        return bool(await asyncio.wait_for(coro, limit))
    except Exception:
        return False


async def _app_db_ok() -> bool:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def _provider_ok(configured: bool) -> bool:
    if not configured:
        return False
    now = time.monotonic()
    if now - float(_provider_probe_cache["at"]) < 30:
        return bool(_provider_probe_cache["ok"])
    ok = await _check(get_llm().probe(), limit=6)
    _provider_probe_cache.update(at=now, ok=ok)
    return ok


@router.get("/health/ready", response_model=Readiness, responses={503: {"model": Readiness}})
async def readiness(request: Request):
    settings = request.app.state.settings
    app_db, analytics_db, cache, provider = await asyncio.gather(
        _check(_app_db_ok()), _check(analytics_pool.ping()), _check(cache_ping()), _provider_ok(settings.llm_configured)
    )
    # the provider is informational: dashboards must keep working during a provider outage
    ready = app_db and analytics_db
    body = Readiness(
        status="ready" if ready else "degraded",
        app_db=app_db,
        analytics_db=analytics_db,
        cache=cache,
        provider=provider,
        provider_configured=settings.llm_configured,
    )
    return JSONResponse(status_code=200 if ready else 503, content=body.model_dump())
