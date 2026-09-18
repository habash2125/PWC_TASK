"""POST /chat — one conversational turn; POST /turns/{id}/feedback."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import DbSession, SettingsDep, rate_limit, request_meta, require_role
from app.api.errors import NotFoundError
from app.api.routers.sessions import turn_out
from app.api.schemas.chat import ChatRequest, FeedbackOut, FeedbackRequest, TurnOut
from app.core.auth.rbac import Principal
from app.core.chat.pipeline import run_turn
from app.db.models import UserRole
from app.db.repos.chat import ChatRepo
from app.observability.request_context import get_llm_counter

router = APIRouter(tags=["chat"])
Analyst = Annotated[Principal, Depends(require_role(UserRole.analyst))]


@router.post("/chat", response_model=TurnOut, dependencies=[Depends(rate_limit("chat"))])
async def chat(
    body: ChatRequest, principal: Analyst, session: DbSession, settings: SettingsDep, request: Request
) -> TurnOut:
    repo = ChatRepo(session)
    chat_session = await repo.get_session(body.session_id, user_id=principal.id)
    if chat_session is None:
        raise NotFoundError("Session not found")
    result = await run_turn(session, settings, principal, chat_session, body.message, meta=request_meta(request))
    await session.refresh(result.turn, attribute_names=["charts"])
    return turn_out(result.turn, result.guard_events, result.chart_order, llm_calls=get_llm_counter().calls)


@router.post("/turns/{turn_id}/feedback", response_model=FeedbackOut, status_code=201)
async def feedback(turn_id: uuid.UUID, body: FeedbackRequest, principal: Analyst, session: DbSession) -> FeedbackOut:
    """Thumbs up/down stored with the turn's trace_id — an offline review queue of real failures."""
    if body.rating == 0:
        raise NotFoundError("rating must be -1 or 1", code="validation_failed")
    repo = ChatRepo(session)
    turn = await repo.get_turn(turn_id, user_id=principal.id)
    if turn is None:
        raise NotFoundError("Turn not found")
    row = await repo.add_feedback(turn_id=turn.id, user_id=principal.id, rating=body.rating, comment=body.comment)
    return FeedbackOut(id=row.id, turn_id=turn.id, rating=row.rating, trace_id=turn.trace_id)
