"""Dashboard refresh: deterministic replay of a chart artefact with **zero model calls**.

Refresh is not a cached result and not a re-ask of the question.  For the
principal requesting it:

1. fetch *their* scope (fail-closed);
2. run the stored SQL through the same guard as a live turn (parser enforces, no LLM),
   which binds *their* scope values;
3. execute on the read-only pool;
4. re-execute the stored ``render_code`` in the sandbox against the fresh
   DataFrame to produce a new ``{data, layout}``;
5. record a ``tile_refresh`` row and any guard event.

A drift in ``sql_hash`` between the saved chart and what the guard emitted is
reported; a query that no longer runs is reported as ``invalid_query`` with the
stored SQL as the pointer — never a silently wrong number.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ScopeUnavailable
from app.api.schemas.dashboards import DashboardRefreshOut, TileRefreshOut
from app.config import get_settings
from app.core.auth.rbac import Principal
from app.core.cache import get_redis
from app.core.runtime.agent_loop import _json_safe, _to_frame
from app.core.runtime.python_exec import run_python
from app.core.security.access_scope import DEFAULT_SCOPE_KEY, DbScopeSource
from app.core.sql.schema_context import load_allow_list
from app.core.sql.sql_guard import SqlGuard
from app.db.analytics_pool import AnalyticsQueryError, QueryResult, execute_readonly
from app.db.models import DashboardTile, GuardKind, GuardVerdict, RefreshStatus, SavedChart
from app.db.repos.chat import ChatRepo
from app.db.repos.dashboards import DashboardRepo
from app.db.session import session_factory
from app.observability import metrics
from app.observability.request_context import get_llm_counter, trace_id_var
from app.observability.tracing import root_span, stage_span

log = logging.getLogger("lens.sql")


@dataclass(slots=True)
class RefreshOutcome:
    status: RefreshStatus
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int | None = None
    truncated: bool = False
    duration_ms: int = 0
    sql_hash: str | None = None
    drifted: bool = False
    error_code: str | None = None
    message: str | None = None
    trace_id: str | None = None
    chart_spec: dict[str, Any] | None = None
    scope_applied: str = ""
    llm_calls: int = 0
    guard_event: dict[str, Any] | None = None
    cached: bool = False


async def _cached_result(key: str) -> QueryResult | None:
    redis = get_redis()
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
    except Exception:
        return None
    if not raw:
        return None
    payload = json.loads(raw)
    return QueryResult(
        columns=payload["columns"],
        rows=[tuple(r) for r in payload["rows"]],
        duration_ms=0,
        truncated=payload["truncated"],
    )


async def _store_result(key: str, result: QueryResult, ttl: int) -> None:
    redis = get_redis()
    if redis is None or ttl <= 0:
        return
    try:
        payload = {
            "columns": result.columns,
            "rows": [[_json_safe(v) for v in r] for r in result.rows],
            "truncated": result.truncated,
        }
        await redis.set(key, json.dumps(payload, default=str), ex=ttl)
    except Exception:
        pass


async def run_saved_chart(
    session: AsyncSession, principal: Principal, chart: SavedChart, *, tile: DashboardTile | None = None
) -> RefreshOutcome:
    """Executes a chart artefact for ``principal``.  Opens its own trace so every refresh is inspectable."""
    settings = get_settings()
    counter = get_llm_counter()
    calls_before = counter.calls
    started = time.perf_counter()
    with root_span(
        "tile.refresh", chart_id=str(chart.id), tile_id=str(tile.id) if tile else None, viewer_id=str(principal.id)
    ) as span:
        trace_id = trace_id_var.get()
        outcome = RefreshOutcome(status=RefreshStatus.ok, trace_id=trace_id)

        # 1. the viewer's scope, fail-closed
        with stage_span("scope"):
            try:
                scope = await DbScopeSource(session_factory()).fetch(
                    principal.id, chart.data_source_id, DEFAULT_SCOPE_KEY
                )
            except ScopeUnavailable as exc:
                outcome.status, outcome.error_code, outcome.message = (
                    RefreshStatus.error,
                    "scope_unavailable",
                    exc.detail,
                )
                outcome.guard_event = {"kind": "scope", "verdict": "blocked", "reason": "scope source unavailable"}
                return _finish(outcome, started, counter.calls - calls_before, span)
        outcome.scope_applied = scope.describe()

        # 2. the same guard as a live turn; parser enforces (no model on this path)
        with stage_span("sql.guard") as gspan:
            try:
                allow = await load_allow_list(session, chart.data_source_id)
            except ValueError:
                outcome.status, outcome.error_code, outcome.message = (
                    RefreshStatus.invalid_query,
                    "source_unavailable",
                    "The data source is no longer available",
                )
                return _finish(outcome, started, counter.calls - calls_before, span)
            guard = await SqlGuard(settings).check(chart.sql_text, allow=allow, scope=scope, mode="refresh")
            gspan.set_attribute("verdict", guard.verdict)
            gspan.set_attribute("reason", guard.reason[:300])
        if guard.verdict != "allowed":
            outcome.guard_event = {
                "kind": guard.kind,
                "verdict": guard.verdict,
                "reason": guard.reason,
                "offending_sql": chart.sql_text,
                "shadow_parser_verdict": guard.shadow_parser_verdict,
            }
        if guard.blocked:
            outcome.status, outcome.error_code, outcome.message = RefreshStatus.blocked, "guard_blocked", guard.reason
            return _finish(outcome, started, counter.calls - calls_before, span)
        assert guard.sql_bound and guard.sql_hash
        outcome.sql_hash = guard.sql_hash
        outcome.drifted = guard.sql_hash != chart.sql_hash

        # 3. execute (short-lived cache keyed on the *bound* SQL, so scope is part of the key)
        cache_key = "q:" + hashlib.sha256(guard.sql_bound.encode()).hexdigest()
        with stage_span("sql.execute", sql_hash=guard.sql_hash) as espan:
            result = await _cached_result(cache_key)
            outcome.cached = result is not None
            if result is None:
                try:
                    result = await execute_readonly(
                        guard.sql_bound,
                        statement_timeout_ms=settings.sql_statement_timeout_ms,
                        max_rows=settings.sql_max_rows,
                    )
                except AnalyticsQueryError as exc:
                    espan.set_attribute("error", exc.code)
                    outcome.status, outcome.error_code = RefreshStatus.invalid_query, exc.code
                    outcome.message = f"Query no longer valid against the current views ({exc}). Review the stored SQL."
                    return _finish(outcome, started, counter.calls - calls_before, span)
                await _store_result(cache_key, result, settings.query_cache_ttl_seconds)
            espan.set_attribute("row_count", result.row_count)
            espan.set_attribute("cached", outcome.cached)
        outcome.columns = result.columns
        outcome.rows = [[_json_safe(v) for v in r] for r in result.rows]
        outcome.row_count = result.row_count
        outcome.truncated = result.truncated

        # 4. deterministic re-render of the stored figure code
        if chart.render_code:
            with stage_span("chart.render") as rspan:
                df = _to_frame(result.columns, result.rows)
                exec_result = await run_python(
                    chart.render_code,
                    {chart.dataset_name: df},
                    timeout_seconds=settings.py_exec_timeout_seconds,
                    memory_mb=settings.py_exec_memory_mb,
                    cpu_seconds=settings.py_exec_cpu_seconds,
                    scratch_root=settings.py_exec_scratch_dir,
                )
                rspan.set_attribute("ok", exec_result.ok)
                rspan.set_attribute("figures", len(exec_result.figures))
                if exec_result.ok and exec_result.figures:
                    outcome.chart_spec = exec_result.figures[0].spec.model_dump()
                else:
                    metrics.chart_capture_failures.labels("refresh_render").inc()
                    outcome.message = (
                        "Data refreshed, but the chart could not be re-rendered: "
                        + (exec_result.error or "no figure produced")[:300]
                    )
        return _finish(outcome, started, counter.calls - calls_before, span)


def _finish(outcome: RefreshOutcome, started: float, llm_calls: int, span) -> RefreshOutcome:
    outcome.duration_ms = int((time.perf_counter() - started) * 1000)
    outcome.llm_calls = llm_calls
    span.set_attribute("status", outcome.status.value)
    span.set_attribute("llm_calls", llm_calls)
    span.set_attribute("row_count", outcome.row_count or 0)
    metrics.tile_refresh_latency.observe(outcome.duration_ms / 1000)
    metrics.tile_refresh_status.labels(outcome.status.value).inc()
    if outcome.guard_event and outcome.guard_event["verdict"] == "blocked":
        metrics.guard_blocks.labels(outcome.guard_event["kind"], "refresh").inc()
    return outcome


async def refresh_tile(session: AsyncSession, principal: Principal, tile: DashboardTile) -> TileRefreshOut:
    outcome = await run_saved_chart(session, principal, tile.chart, tile=tile)
    repo = DashboardRepo(session)
    row = await repo.record_refresh(
        tile_id=tile.id,
        viewer_id=principal.id,
        status=outcome.status,
        row_count=outcome.row_count,
        duration_ms=outcome.duration_ms,
        sql_hash=outcome.sql_hash,
        error_code=outcome.error_code,
        trace_id=outcome.trace_id,
    )
    if outcome.guard_event:
        ev = outcome.guard_event
        if ev["verdict"] == "blocked":
            from app.db.repos.audit import AuditRepo

            await AuditRepo(session).write(
                action="guard.block",
                tenant_id=principal.tenant_id,
                actor_user_id=principal.id,
                object_type="dashboard_tile",
                object_id=tile.id,
                metadata={"kind": ev["kind"], "reason": ev["reason"][:300], "surface": "refresh"},
            )
        await ChatRepo(session).add_guard_event(
            turn_id=None,
            tile_refresh_id=row.id,
            kind=GuardKind(ev["kind"]),
            verdict=GuardVerdict(ev["verdict"]),
            reason=ev["reason"],
            offending_sql=ev.get("offending_sql"),
            shadow=ev.get("shadow_parser_verdict"),
        )
    log.info(
        "tile refreshed",
        extra={
            "tile_id": str(tile.id),
            "status": outcome.status.value,
            "row_count": outcome.row_count,
            "duration_ms": outcome.duration_ms,
            "llm_calls": outcome.llm_calls,
            "drifted": outcome.drifted,
        },
    )
    return TileRefreshOut(
        tile_id=tile.id,
        status=outcome.status,
        columns=outcome.columns,
        rows=outcome.rows,
        row_count=outcome.row_count,
        truncated=outcome.truncated,
        duration_ms=outcome.duration_ms,
        sql_hash=outcome.sql_hash,
        drifted=outcome.drifted,
        error_code=outcome.error_code,
        message=outcome.message,
        trace_id=outcome.trace_id,
        llm_calls=outcome.llm_calls,
        chart_spec=outcome.chart_spec,
        refreshed_at=datetime.now(UTC),
    )


async def refresh_dashboard(
    session: AsyncSession, principal: Principal, dashboard_id: uuid.UUID
) -> DashboardRefreshOut:
    """All tiles with bounded concurrency.  Each tile is its own trace and its own tile_refresh row."""
    settings = get_settings()
    tiles = await DashboardRepo(session).tiles(dashboard_id)
    sem = asyncio.Semaphore(settings.refresh_concurrency)
    results: list[TileRefreshOut] = []

    async def _one(tile: DashboardTile) -> TileRefreshOut:
        async with sem:
            # each tile records into its own session so a failure in one never poisons the others
            async with session_factory()() as s:
                tile_row = await DashboardRepo(s).tile(dashboard_id, tile.id)
                assert tile_row is not None
                out = await refresh_tile(s, principal, tile_row)
                await s.commit()
                return out

    for coro in asyncio.as_completed([_one(t) for t in tiles]):
        results.append(await coro)
    order = {t.id: i for i, t in enumerate(tiles)}
    results.sort(key=lambda r: order.get(r.tile_id, 0))
    return DashboardRefreshOut(dashboard_id=dashboard_id, tiles=results, llm_calls=sum(r.llm_calls for r in results))
