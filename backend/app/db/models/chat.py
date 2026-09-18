"""chat_session, turn, turn_chart, guard_event, feedback, trace_span."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base, GuardKind, GuardVerdict, TurnStatus, ts_created, uuid_pk


def _enum(cls, name: str):
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class ChatSession(Base):
    __tablename__ = "chat_session"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("data_source.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts_created()
    last_active_at: Mapped[datetime] = ts_created()


class Turn(Base):
    __tablename__ = "turn"
    __table_args__ = (
        Index("ix_turn_session_created", "session_id", "created_at"),
        Index("ix_turn_trace_id", "trace_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_session.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )  # audit survives session deletion
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer_markdown: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TurnStatus] = mapped_column(_enum(TurnStatus, "turn_status"), nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    sql_text: Mapped[str | None] = mapped_column(Text)
    prompt_version_id: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    stage_timings: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = ts_created()

    charts: Mapped[list[TurnChart]] = relationship(
        back_populates="turn", cascade="all, delete-orphan", order_by="TurnChart.position"
    )


class TurnChart(Base):
    """Charts produced by a turn *before* anyone pins them.  Pinning copies a rendered artefact."""

    __tablename__ = "turn_chart"
    __table_args__ = (UniqueConstraint("turn_id", "position", name="uq_turn_chart_turn_position"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    turn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("turn.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    chart_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sql_text: Mapped[str] = mapped_column(Text, nullable=False)
    sql_hash: Mapped[str] = mapped_column(Text, nullable=False)
    render_code: Mapped[str | None] = mapped_column(Text)  # the Python that built the figure from `dataset_name`
    dataset_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="df")

    turn: Mapped[Turn] = relationship(back_populates="charts")


class GuardEvent(Base):
    __tablename__ = "guard_event"
    __table_args__ = (Index("ix_guard_event_created", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("turn.id", ondelete="CASCADE"))
    tile_refresh_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tile_refresh.id", ondelete="CASCADE")
    )
    kind: Mapped[GuardKind] = mapped_column(_enum(GuardKind, "guard_kind"), nullable=False)
    verdict: Mapped[GuardVerdict] = mapped_column(_enum(GuardVerdict, "guard_verdict"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    offending_sql: Mapped[str | None] = mapped_column(Text)
    shadow_parser_verdict: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts_created()


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (CheckConstraint("rating IN (-1, 1)", name="rating_sign"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    turn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("turn.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts_created()


class TraceSpan(Base):
    __tablename__ = "trace_span"
    __table_args__ = (Index("ix_trace_span_trace_id", "trace_id"), Index("ix_trace_span_start_ts", "start_ts"))

    span_id: Mapped[str] = mapped_column(Text, primary_key=True)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    start_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
