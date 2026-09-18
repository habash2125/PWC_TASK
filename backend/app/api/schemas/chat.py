from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.api.schemas import Out, Strict
from app.db.models import TurnStatus


class SessionCreate(Strict):
    data_source_id: uuid.UUID | None = None  # defaults to the tenant's first active source
    title: str | None = Field(default=None, max_length=120)


class SessionOut(Out):
    id: uuid.UUID
    data_source_id: uuid.UUID
    title: str | None
    created_at: datetime
    last_active_at: datetime


class ChatRequest(Strict):
    session_id: uuid.UUID
    message: str = Field(min_length=1, max_length=4000)


class TurnChartOut(Out):
    id: uuid.UUID
    position: int
    title: str
    chart_spec: dict[str, Any]
    sql_text: str
    sql_hash: str


class GuardEventOut(Out):
    kind: str
    verdict: str
    reason: str
    shadow_parser_verdict: str | None = None


class TurnOut(Out):
    id: uuid.UUID
    session_id: uuid.UUID
    question: str
    answer_markdown: str | None
    status: TurnStatus
    error_code: str | None
    sql_text: str | None
    charts: list[TurnChartOut]
    chart_order: list[int] = []
    guard_events: list[GuardEventOut] = []
    model: str | None
    prompt_version_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    duration_ms: int | None
    stage_timings: dict[str, int] | None
    trace_id: str
    llm_calls: int | None = None  # known for the request that produced the turn; None on later reads
    created_at: datetime


class FeedbackRequest(Strict):
    rating: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackOut(Out):
    id: uuid.UUID
    turn_id: uuid.UUID
    rating: int
    trace_id: str
