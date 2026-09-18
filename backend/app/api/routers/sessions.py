"""Chat sessions and their turns."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, DbSession, require_role
from app.api.errors import NotFoundError, ValidationFailed
from app.api.schemas.chat import GuardEventOut, SessionCreate, SessionOut, TurnChartOut, TurnOut
from app.core.auth.rbac import Principal
from app.db.models import DataSource, Turn, UserRole
from app.db.repos.chat import ChatRepo

router = APIRouter(prefix="/sessions", tags=["chat"])
Analyst = Annotated[Principal, Depends(require_role(UserRole.analyst))]


def turn_out(
    turn: Turn, guard_events=(), chart_order: list[int] | None = None, llm_calls: int | None = None
) -> TurnOut:
    charts = [TurnChartOut.model_validate(c) for c in sorted(turn.charts, key=lambda c: c.position)]
    return TurnOut(
        id=turn.id,
        session_id=turn.session_id,
        question=turn.question,
        answer_markdown=turn.answer_markdown,
        status=turn.status,
        error_code=turn.error_code,
        sql_text=turn.sql_text,
        charts=charts,
        chart_order=chart_order or list(range(1, len(charts) + 1)),
        guard_events=[
            GuardEventOut(
                kind=str(getattr(e, "kind", e["kind"]) if isinstance(e, dict) else e.kind.value),
                verdict=str(e["verdict"] if isinstance(e, dict) else e.verdict.value),
                reason=e["reason"] if isinstance(e, dict) else e.reason,
                shadow_parser_verdict=(
                    e.get("shadow_parser_verdict") if isinstance(e, dict) else e.shadow_parser_verdict
                ),
            )
            for e in guard_events
        ],
        model=turn.model,
        prompt_version_id=turn.prompt_version_id,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        cost_usd=float(turn.cost_usd) if turn.cost_usd is not None else None,
        duration_ms=turn.duration_ms,
        stage_timings=turn.stage_timings,
        trace_id=turn.trace_id,
        llm_calls=llm_calls,
        created_at=turn.created_at,
    )


@router.post("", response_model=SessionOut, status_code=201)
async def create_session(body: SessionCreate, principal: Analyst, session: DbSession) -> SessionOut:
    source_id = body.data_source_id
    if source_id is None:
        source = (
            (
                await session.execute(
                    select(DataSource)
                    .where(DataSource.tenant_id == principal.tenant_id, DataSource.is_active.is_(True))
                    .order_by(DataSource.created_at)
                )
            )
            .scalars()
            .first()
        )
        if source is None:
            raise ValidationFailed("No active data source in your tenant")
        source_id = source.id
    else:
        source = await session.get(DataSource, source_id)
        if source is None or source.tenant_id != principal.tenant_id or not source.is_active:
            raise NotFoundError("Data source not found")
    row = await ChatRepo(session).create_session(user_id=principal.id, data_source_id=source_id, title=body.title)
    return SessionOut.model_validate(row)


@router.get("", response_model=list[SessionOut])
async def list_sessions(principal: CurrentPrincipal, session: DbSession) -> list[SessionOut]:
    return [SessionOut.model_validate(s) for s in await ChatRepo(session).list_sessions(principal.id)]


@router.get("/{session_id}/turns", response_model=list[TurnOut])
async def list_turns(session_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession) -> list[TurnOut]:
    repo = ChatRepo(session)
    chat = await repo.get_session(session_id, user_id=principal.id)
    if chat is None:
        raise NotFoundError("Session not found")
    out = []
    for t in await repo.list_turns(chat.id):
        out.append(turn_out(t, await repo.guard_events_for_turn(t.id)))
    return out
