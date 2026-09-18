"""Charts — a first-class resource, independent of dashboards."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import CurrentPrincipal, DbSession, IdempotencyDep, rate_limit, request_meta, require_role
from app.api.errors import ConflictError, NotFoundError
from app.api.schemas.charts import (
    ChartCreate,
    ChartListOut,
    ChartOut,
    ChartRunOut,
    ChartSummaryOut,
    ChartUpdate,
    ChartUsageItem,
    ChartUsageOut,
)
from app.core.auth.rbac import Principal
from app.core.dashboards import charts as chart_service
from app.db.models import UserRole
from app.db.repos.charts import ChartRepo

router = APIRouter(prefix="/charts", tags=["charts"])

Analyst = Annotated[Principal, Depends(require_role(UserRole.analyst))]


@router.get("", response_model=ChartListOut)
async def list_charts(
    principal: CurrentPrincipal,
    session: DbSession,
    search: Annotated[str | None, Query(max_length=120)] = None,
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ChartListOut:
    repo = ChartRepo(session)
    rows, total = await repo.list_for_owner(
        principal.id, include_archived=include_archived, search=search, limit=limit, offset=offset
    )
    items = []
    for c in rows:
        items.append(
            ChartSummaryOut(
                id=c.id,
                title=c.title,
                description=c.description,
                question=c.question,
                sql_hash=c.sql_hash,
                version=c.version,
                is_archived=c.is_archived,
                placement_count=await repo.placement_count(c.id),
                created_at=c.created_at,
                updated_at=c.updated_at,
            )
        )
    return ChartListOut(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=ChartOut, status_code=201)
async def pin_chart(body: ChartCreate, principal: Analyst, session: DbSession, request: Request, idem: IdempotencyDep):
    """The pin action: copies a rendered ``turn_chart`` (SQL + spec) into the caller's library. No model call."""
    if (replay := await idem.replay()) is not None:
        return replay
    chart = await chart_service.pin_turn_chart(session, principal, body, meta=request_meta(request))
    out = ChartOut.model_validate(chart)
    await idem.store(201, out.model_dump(mode="json"))
    return out


@router.get("/{chart_id}", response_model=ChartOut)
async def get_chart(chart_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession) -> ChartOut:
    chart = await chart_service.get_visible_chart(session, principal, chart_id)
    return ChartOut.model_validate(chart)


@router.patch("/{chart_id}", response_model=ChartOut)
async def update_chart(
    chart_id: uuid.UUID, body: ChartUpdate, principal: Analyst, session: DbSession, request: Request
) -> ChartOut:
    chart = await chart_service.update_chart(session, principal, chart_id, body, meta=request_meta(request))
    return ChartOut.model_validate(chart)


@router.delete("/{chart_id}", status_code=204, response_model=None)
async def delete_chart(chart_id: uuid.UUID, principal: Analyst, session: DbSession, request: Request) -> None:
    """409 if the chart is placed on any dashboard — the response lists them.  Archive instead."""
    await chart_service.delete_chart(session, principal, chart_id, meta=request_meta(request))


@router.get("/{chart_id}/usage", response_model=ChartUsageOut)
async def chart_usage(chart_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession) -> ChartUsageOut:
    chart = await chart_service.get_visible_chart(session, principal, chart_id)
    placements = await ChartRepo(session).usage(chart.id)
    return ChartUsageOut(
        chart_id=chart.id,
        placements=[ChartUsageItem(**p) for p in placements],
        dashboard_count=len({p["dashboard_id"] for p in placements}),
    )


@router.post("/{chart_id}/run", response_model=ChartRunOut, dependencies=[Depends(rate_limit("run"))])
async def run_chart(chart_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession) -> ChartRunOut:
    """Executes the stored SQL under the *caller's* scope.  Zero model calls."""
    from app.core.dashboards.refresh import run_saved_chart

    chart = await chart_service.get_visible_chart(session, principal, chart_id)
    result = await run_saved_chart(session, principal, chart)
    if result.status.value != "ok":
        if result.status.value == "blocked":
            raise ConflictError(result.message or "Blocked by the guard", code="guard_blocked")
        raise NotFoundError(result.message or "Query no longer valid", code=result.status.value)
    return ChartRunOut(
        chart_id=chart.id,
        columns=result.columns,
        rows=result.rows,
        row_count=result.row_count or 0,
        truncated=result.truncated,
        duration_ms=result.duration_ms,
        sql_executed_hash=result.sql_hash or "",
        scope_applied=result.scope_applied,
        trace_id=result.trace_id,
        chart_spec=result.chart_spec,
        llm_calls=result.llm_calls,
    )
