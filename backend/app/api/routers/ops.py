"""Ops: trace viewer, usage/cost accounting, Prometheus metrics, tenant users."""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, DbSession, SettingsDep, require_role
from app.api.errors import ForbiddenError, NotFoundError
from app.api.schemas import Out
from app.api.schemas.auth import UserOut
from app.core.auth.rbac import Principal
from app.core.cache import get_redis
from app.db.models import GuardEvent, TraceSpan, Turn, UserRole
from app.db.repos.chat import ChatRepo
from app.db.repos.users import UserRepo
from app.observability import metrics
from app.observability.tracing import flush_spans

router = APIRouter(tags=["ops"])
Admin = Annotated[Principal, Depends(require_role(UserRole.admin))]


class SpanOut(Out):
    span_id: str
    parent_span_id: str | None
    name: str
    start_ts: datetime | None
    end_ts: datetime | None
    duration_ms: int | None
    status: str | None
    attributes: dict[str, Any]
    children: list[SpanOut] = []


class TraceTurnOut(Out):
    id: uuid.UUID
    question: str
    status: str
    error_code: str | None
    model: str | None
    prompt_version_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    duration_ms: int | None
    stage_timings: dict[str, int] | None


class TraceGuardEventOut(Out):
    kind: str
    verdict: str
    reason: str
    offending_sql: str | None
    shadow_parser_verdict: str | None
    created_at: datetime


class TraceOut(Out):
    trace_id: str
    span_count: int
    llm_calls: int
    total_duration_ms: int | None
    roots: list[SpanOut]
    turn: TraceTurnOut | None
    guard_events: list[TraceGuardEventOut]


class UsageRowOut(Out):
    user_id: uuid.UUID
    email: str | None
    day: date
    turn_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


def build_tree(rows: list[TraceSpan]) -> list[SpanOut]:
    """Nesting from parent ids, sibling order from timestamps.  No stage registry."""
    nodes = {
        r.span_id: SpanOut(
            span_id=r.span_id,
            parent_span_id=r.parent_span_id,
            name=r.name,
            start_ts=r.start_ts,
            end_ts=r.end_ts,
            duration_ms=r.duration_ms,
            status=r.status,
            attributes=r.attributes or {},
            children=[],
        )
        for r in rows
    }
    roots: list[SpanOut] = []
    for node in nodes.values():
        parent = nodes.get(node.parent_span_id or "")
        (parent.children if parent else roots).append(node)

    def _sort(items: list[SpanOut]) -> None:
        items.sort(key=lambda s: s.start_ts.timestamp() if s.start_ts else 0.0)
        for i in items:
            _sort(i.children)

    _sort(roots)
    return roots


@router.get("/traces/{trace_id}", response_model=TraceOut)
async def trace(trace_id: str, session: DbSession, principal: CurrentPrincipal) -> TraceOut:
    """Admins read any trace; a user may read the traces of their own turns."""
    if len(trace_id) > 64:
        raise NotFoundError("Trace not found")
    await flush_spans()
    from app.db.models import TileRefresh

    turn = (await session.execute(select(Turn).where(Turn.trace_id == trace_id))).scalars().first()
    refresh = (await session.execute(select(TileRefresh).where(TileRefresh.trace_id == trace_id))).scalars().first()
    if principal.role is not UserRole.admin:
        owns_turn = turn is not None and turn.user_id == principal.id
        owns_refresh = refresh is not None and refresh.viewer_id == principal.id
        if not (owns_turn or owns_refresh):
            raise NotFoundError("Trace not found")
    rows = list(
        (
            await session.execute(select(TraceSpan).where(TraceSpan.trace_id == trace_id).order_by(TraceSpan.start_ts))
        ).scalars()
    )
    if not rows and turn is None and refresh is None:
        raise NotFoundError("Trace not found")
    roots = build_tree(rows)
    llm_calls = sum(1 for r in rows if r.name.startswith("llm."))
    total = max((r.duration_ms or 0) for r in rows) if rows else None
    events: list[GuardEvent] = []
    if turn is not None:
        events = await ChatRepo(session).guard_events_for_turn(turn.id)
    elif refresh is not None:
        events = list(
            (await session.execute(select(GuardEvent).where(GuardEvent.tile_refresh_id == refresh.id))).scalars()
        )
    return TraceOut(
        trace_id=trace_id,
        span_count=len(rows),
        llm_calls=llm_calls,
        total_duration_ms=total,
        roots=roots,
        turn=TraceTurnOut(
            id=turn.id,
            question=turn.question,
            status=turn.status.value,
            error_code=turn.error_code,
            model=turn.model,
            prompt_version_id=turn.prompt_version_id,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            cost_usd=float(turn.cost_usd) if turn.cost_usd is not None else None,
            duration_ms=turn.duration_ms,
            stage_timings=turn.stage_timings,
        )
        if turn
        else None,
        guard_events=[
            TraceGuardEventOut(
                kind=e.kind.value,
                verdict=e.verdict.value,
                reason=e.reason,
                offending_sql=e.offending_sql,
                shadow_parser_verdict=e.shadow_parser_verdict,
                created_at=e.created_at,
            )
            for e in events
        ],
    )


@router.get("/usage", response_model=list[UsageRowOut])
async def usage(
    session: DbSession,
    principal: CurrentPrincipal,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
    user_id: uuid.UUID | None = None,
) -> list[UsageRowOut]:
    """Per-user cost per day.  Admins see everyone; everyone else sees themselves."""
    if principal.role is not UserRole.admin:
        if user_id is not None and user_id != principal.id:
            raise ForbiddenError("You can only see your own usage")
        user_id = principal.id
    rows = await ChatRepo(session).usage_rows(user_id=user_id, days=days)
    users = {u.id: u.email for u in await UserRepo(session).list_in_tenant(principal.tenant_id)}
    return [
        UsageRowOut(
            user_id=r.user_id,
            email=users.get(r.user_id),
            day=r.day,
            turn_count=r.turn_count,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cost_usd=float(r.cost_usd),
        )
        for r in rows
    ]


@router.get("/metrics", include_in_schema=False)
async def prometheus(settings: SettingsDep) -> Response:
    if not settings.metrics_enabled:
        raise NotFoundError("metrics disabled")
    redis = get_redis()
    if redis is not None:
        try:
            metrics.active_users.set(await redis.scard(f"active:{int(time.time() // 300)}"))
        except Exception:
            pass
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/users", response_model=list[UserOut])
async def list_users(session: DbSession, principal: CurrentPrincipal) -> list[UserOut]:
    """Tenant directory (for the share dialog).  Everyone in the tenant can see names and roles."""
    return [
        UserOut.model_validate(u) for u in await UserRepo(session).list_in_tenant(principal.tenant_id) if u.is_active
    ]
