"""Read-only engine for the analytics database.  Raw SQL only — no ORM models.

The role behind this engine is ``lens_readonly``: ``SELECT`` on the allow-listed
``v_*`` views and nothing else.  The database is the last line of defence, not
the prompt, so even a guard bug cannot reach a base table.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings
from app.observability import metrics

log = logging.getLogger("lens.sql")

_engine: AsyncEngine | None = None


class AnalyticsQueryError(Exception):
    """A database-level failure executing a guarded statement (surfaced to the agent as text)."""

    def __init__(self, message: str, *, code: str = "db_error"):
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    duration_ms: int
    truncated: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)


def init_analytics_db(settings: Settings) -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.analytics_readonly_url,
            pool_size=settings.analytics_db_pool_size,
            max_overflow=4,
            pool_pre_ping=True,
            pool_recycle=1800,
            # every connection is opened read-only; the role is SELECT-only anyway
            connect_args={
                "server_settings": {"default_transaction_read_only": "on", "application_name": "lens-readonly"}
            },
        )
    return _engine


def get_analytics_engine() -> AsyncEngine:
    assert _engine is not None, "analytics db not initialised"
    return _engine


async def dispose_analytics_db() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
    _engine = None


def _sanitise_db_error(exc: BaseException) -> str:
    """Strip anything that looks like connection detail; keep the SQLSTATE message for self-correction."""
    msg = str(getattr(exc, "orig", exc))
    msg = msg.split("\n")[0]
    return msg[:500]


async def execute_readonly(sql: str, *, statement_timeout_ms: int, max_rows: int) -> QueryResult:
    """Run one guarded statement under a statement timeout inside a read-only transaction."""
    engine = get_analytics_engine()
    started = time.perf_counter()
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))
                await conn.execute(text("SET LOCAL transaction_read_only = on"))
                result = await conn.execute(text(sql))
                columns = list(result.keys())
                fetched = result.fetchmany(max_rows + 1)
                truncated = len(fetched) > max_rows
                rows = [tuple(r) for r in fetched[:max_rows]]
    except DBAPIError as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        message = _sanitise_db_error(exc)
        code = "timeout" if "statement timeout" in message.lower() else "db_error"
        metrics.sql_executions.labels(code).inc()
        log.warning("sql failed", extra={"duration_ms": elapsed, "code": code, "db_message": message})
        raise AnalyticsQueryError(message, code=code) from None
    elapsed = int((time.perf_counter() - started) * 1000)
    metrics.sql_executions.labels("ok").inc()
    metrics.sql_latency.observe(elapsed / 1000)
    log.info("sql ok", extra={"duration_ms": elapsed, "row_count": len(rows), "truncated": truncated})
    return QueryResult(columns=columns, rows=rows, duration_ms=elapsed, truncated=truncated)


async def ping() -> bool:
    engine = get_analytics_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
