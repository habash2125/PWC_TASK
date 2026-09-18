from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.api.schemas import Out, Strict


class ChartSpec(Strict):
    """A Plotly figure captured from a real ``Figure`` object: ``{data, layout}``."""

    data: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    layout: dict[str, Any] = Field(default_factory=dict)


class ChartCreate(Strict):
    """The "pin" action: copies an already-rendered ``turn_chart`` into the library."""

    turn_chart_id: uuid.UUID
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ChartUpdate(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    params: dict[str, Any] | None = None
    is_archived: bool | None = None


class ChartOut(Out):
    id: uuid.UUID
    owner_id: uuid.UUID
    data_source_id: uuid.UUID
    title: str
    description: str | None
    question: str
    sql_text: str
    sql_hash: str
    chart_spec: dict[str, Any]
    params: dict[str, Any]
    source_turn_id: uuid.UUID | None
    version: int
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class ChartSummaryOut(Out):
    id: uuid.UUID
    title: str
    description: str | None
    question: str
    sql_hash: str
    version: int
    is_archived: bool
    placement_count: int
    created_at: datetime
    updated_at: datetime


class ChartListOut(Out):
    items: list[ChartSummaryOut]
    total: int
    limit: int
    offset: int


class ChartUsageItem(Out):
    dashboard_id: uuid.UUID
    dashboard_name: str
    owner_id: uuid.UUID
    group_id: uuid.UUID
    group_title: str
    tile_id: uuid.UUID
    title_override: str | None


class ChartUsageOut(Out):
    chart_id: uuid.UUID
    placements: list[ChartUsageItem]
    dashboard_count: int


class ChartRunOut(Out):
    chart_id: uuid.UUID
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    duration_ms: int
    sql_executed_hash: str
    scope_applied: str
    trace_id: str | None
    chart_spec: dict[str, Any] | None = None
    llm_calls: int = 0
