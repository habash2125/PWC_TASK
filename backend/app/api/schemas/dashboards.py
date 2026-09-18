from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.api.schemas import Out, Strict
from app.db.models import DashboardRole, RefreshStatus, Visibility


class DashboardCreate(Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class DashboardUpdate(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    visibility: Visibility | None = None
    is_archived: bool | None = None


class GroupCreate(Strict):
    title: str = Field(min_length=1, max_length=60)


class GroupUpdate(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=60)
    position: int | None = Field(default=None, ge=0, le=500)
    is_collapsed: bool | None = None


class TileCreate(Strict):
    saved_chart_id: uuid.UUID
    group_id: uuid.UUID | None = None  # default group when omitted
    title_override: str | None = Field(default=None, min_length=1, max_length=200)
    w: int = Field(default=6, ge=2, le=12)
    h: int = Field(default=4, ge=2, le=24)


class TileUpdate(Strict):
    group_id: uuid.UUID | None = None
    title_override: str | None = Field(default=None, max_length=200)
    x: int | None = Field(default=None, ge=0, le=11)
    y: int | None = Field(default=None, ge=0, le=10_000)
    w: int | None = Field(default=None, ge=2, le=12)
    h: int | None = Field(default=None, ge=2, le=24)
    overrides: dict[str, Any] | None = None  # presentation only — never SQL

    @field_validator("overrides")
    @classmethod
    def _no_sql_in_overrides(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v and any(k.lower() in {"sql", "sql_text", "query"} for k in v):
            raise ValueError("tile overrides cannot carry SQL; edit the chart instead")
        return v


class LayoutItem(Strict):
    tile_id: uuid.UUID
    group_id: uuid.UUID | None = None
    x: int | None = Field(default=None, ge=0, le=11)
    y: int | None = Field(default=None, ge=0, le=10_000)
    w: int | None = Field(default=None, ge=2, le=12)
    h: int | None = Field(default=None, ge=2, le=24)


class LayoutUpdate(Strict):
    items: list[LayoutItem] = Field(max_length=500)


class GrantItem(Strict):
    principal_id: uuid.UUID
    role: DashboardRole


class GrantsUpdate(Strict):
    grants: list[GrantItem] = Field(min_length=1, max_length=500)


# ── outputs ──────────────────────────────────────────────────────────────────


class TileOut(Out):
    id: uuid.UUID
    dashboard_id: uuid.UUID
    group_id: uuid.UUID
    saved_chart_id: uuid.UUID
    title: str  # resolved: override or the chart's own title
    title_override: str | None
    question: str
    sql_text: str
    sql_hash: str
    chart_spec: dict[str, Any]
    chart_version: int
    position: int
    x: int
    y: int
    w: int
    h: int
    overrides: dict[str, Any]


class GroupOut(Out):
    id: uuid.UUID
    title: str
    position: int
    is_collapsed: bool
    tiles: list[TileOut]


class GrantOut(Out):
    principal_id: uuid.UUID
    email: str | None
    full_name: str | None
    role: DashboardRole
    granted_at: datetime


class DashboardSummaryOut(Out):
    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    visibility: Visibility
    effective_role: DashboardRole
    tile_count: int
    updated_at: datetime


class DashboardOut(Out):
    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    visibility: Visibility
    effective_role: DashboardRole
    is_archived: bool
    groups: list[GroupOut]
    created_at: datetime
    updated_at: datetime


class TileRefreshOut(Out):
    tile_id: uuid.UUID
    status: RefreshStatus
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int | None = None
    truncated: bool = False
    duration_ms: int
    sql_hash: str | None
    drifted: bool = False
    error_code: str | None = None
    message: str | None = None
    trace_id: str | None
    llm_calls: int = 0
    chart_spec: dict[str, Any] | None = None  # re-rendered from the stored render code; None if it failed
    refreshed_at: datetime


class DashboardRefreshOut(Out):
    dashboard_id: uuid.UUID
    tiles: list[TileRefreshOut]
    llm_calls: int


class GroupSuggestionOut(Out):
    title: str
    tile_ids: list[uuid.UUID]


class GroupingProposalOut(Out):
    proposal_id: str
    groups: list[GroupSuggestionOut]
    rationale: str
    diff: list[dict[str, Any]]


class ApplyGroupingRequest(Strict):
    proposal_id: str = Field(min_length=8, max_length=64)
    groups: list[GroupSuggestionOut] = Field(min_length=1, max_length=50)


class ApplyGroupingOut(Out):
    dashboard_id: uuid.UUID
    groups_created: int
    tiles_moved: int
    groups_removed: int
