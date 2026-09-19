"""Read-only engine for the analytics database (SQLite).  Raw SQL only — no ORM models.

SQLite has no roles, so the "SELECT on the ``v_*`` views and nothing else"
guarantee the Postgres role used to give is rebuilt from three SQLite
primitives, all applied on every connection the API opens:

* the file is opened ``mode=ro`` — the OS refuses writes before any SQL runs;
* ``PRAGMA query_only`` — the engine refuses writes, temp tables and ATTACH;
* an **authorizer** callback — SQLite asks it before compiling every table/column
  access.  It allows reads of the allow-listed views, allows the base-table reads
  those views perform internally (the callback is told which view is driving the
  access), and denies everything else: base tables, ``sqlite_master``, PRAGMA,
  ATTACH, extension loading.  See ``Authorizer`` for the one residual.

The database is the last line of defence, not the prompt, so even a guard bug
cannot reach a base table.  The statement timeout is a progress handler that
interrupts the statement once its deadline passes.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings
from app.observability import metrics

log = logging.getLogger("lens.sql")

_engine: AsyncEngine | None = None

# functions that touch the filesystem or the process; none is needed for analytics
FORBIDDEN_SQLITE_FUNCTIONS = frozenset(
    {"load_extension", "readfile", "writefile", "fsdir", "edit", "sqlite_compileoption_used"}
)
_PROGRESS_EVERY_N_OPS = 1000


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


class Authorizer:
    """Per-connection authorizer: reads are allowed only *through* the exposed views.

    SQLite reports, for every column read, the innermost view responsible for it (``source``);
    a base-table read whose source is an exposed view is the view doing its job, a base-table
    read with no source is the caller reaching past the allow-list.

    One wrinkle: after flattening a view into the outer query SQLite re-announces the view's
    base tables once each with an empty column name and no source ("table referenced, no
    column extracted").  Those are accepted when (a) the table is one an exposed view is built
    on — discovered at start-up, see ``discover_base_tables`` — and (b) an exposed view has
    already been named in the current statement (``execute_readonly`` resets that flag around
    every statement).  No value can be extracted through such a read; the residual is that a
    statement which names an exposed view can also count rows of a base table
    (``SELECT COUNT(*) FROM v_x, Products``).  The SQL guard refuses any base-table reference
    long before a statement gets this far.
    """

    __slots__ = ("views", "base_tables", "view_referenced")

    def __init__(self, exposed_views: Iterable[str], base_tables: Iterable[str]):
        self.views = frozenset(v.lower() for v in exposed_views)
        self.base_tables = frozenset(t.lower() for t in base_tables)
        self.view_referenced = False

    def reset(self) -> None:
        self.view_referenced = False

    def __call__(self, action: int, arg1: str | None, arg2: str | None, db_name: str | None, source: str | None) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if (arg2 or "").lower() in FORBIDDEN_SQLITE_FUNCTIONS else sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table = (arg1 or "").lower()
            if table in self.views:
                self.view_referenced = True
                return sqlite3.SQLITE_OK
            if (source or "").lower() in self.views:
                self.view_referenced = True
                return sqlite3.SQLITE_OK
            if arg2 == "" and self.view_referenced and table in self.base_tables:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY  # PRAGMA, ATTACH, DETACH, every write and DDL action


def discover_base_tables(path: Path, exposed_views: Iterable[str]) -> frozenset[str]:
    """Compiles ``SELECT * FROM <view>`` for each exposed view and records the tables it reads."""
    found: set[str] = set()
    views = {v.lower() for v in exposed_views}

    def record(action: int, arg1: str | None, arg2: str | None, _db: str | None, source: str | None) -> int:
        if action == sqlite3.SQLITE_READ and (source or "").lower() in views and arg1:
            found.add(arg1.lower())
        return sqlite3.SQLITE_OK

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.set_authorizer(record)
        for view in exposed_views:
            conn.execute(f"SELECT * FROM {view} LIMIT 0")
    finally:
        conn.close()
    return frozenset(found - views)


def init_analytics_db(settings: Settings, exposed_views: Iterable[str] | None = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        if exposed_views is None:
            from app.db.seed.analytics_catalog import VIEW_NAMES

            exposed_views = VIEW_NAMES
        views = tuple(exposed_views)
        base_tables = discover_base_tables(settings.analytics_sqlite_file, views)
        _engine = create_async_engine(
            settings.analytics_readonly_url,
            pool_size=settings.analytics_db_pool_size,
            max_overflow=2,
            pool_pre_ping=True,
        )

        @event.listens_for(_engine.sync_engine, "connect")
        def _harden(dbapi_connection, record) -> None:
            raw: sqlite3.Connection = dbapi_connection._connection._conn  # SQLAlchemy adapter → aiosqlite → sqlite3
            raw.execute("PRAGMA query_only = 1")
            authorizer = Authorizer(views, base_tables)
            raw.set_authorizer(authorizer)
            record.info["authorizer"] = authorizer

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
    """Strip anything that looks like connection detail; keep the engine message for self-correction."""
    msg = str(getattr(exc, "orig", exc))
    msg = msg.split("\n")[0]
    return msg[:500]


async def execute_readonly(sql: str, *, statement_timeout_ms: int, max_rows: int) -> QueryResult:
    """Run one guarded statement under a statement timeout on a read-only connection."""
    engine = get_analytics_engine()
    started = time.perf_counter()
    deadline = time.monotonic() + statement_timeout_ms / 1000

    def _tick() -> int:  # non-zero aborts the running statement with "interrupted"
        return 1 if time.monotonic() > deadline else 0

    try:
        async with engine.connect() as conn:
            pooled = await conn.get_raw_connection()
            aio = pooled.driver_connection  # the aiosqlite connection
            assert aio is not None
            authorizer: Authorizer = pooled.info["authorizer"]
            authorizer.reset()
            await aio.set_progress_handler(_tick, _PROGRESS_EVERY_N_OPS)
            try:
                result = await conn.execute(text(sql))
                columns = list(result.keys())
                fetched = result.fetchmany(max_rows + 1)
            finally:
                await aio.set_progress_handler(None, 0)
                authorizer.reset()
            truncated = len(fetched) > max_rows
            rows = [tuple(r) for r in fetched[:max_rows]]
    except DBAPIError as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        message = _sanitise_db_error(exc)
        lowered = message.lower()
        if "interrupted" in lowered:
            code, message = "timeout", f"statement timeout after {statement_timeout_ms} ms"
        elif "not authorized" in lowered or "prohibited" in lowered:
            code, message = "denied", f"permission denied: {message}"
        else:
            code = "db_error"
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
