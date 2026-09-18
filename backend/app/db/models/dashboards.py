"""saved_chart, dashboard, dashboard_group, dashboard_tile, dashboard_grant, tile_refresh.

    saved_chart  ──<  dashboard_tile  >──  dashboard_group  >──  dashboard
     (the artefact)   (the placement)        (the section)       (the container)

A chart and a dashboard are separate entities joined by ``dashboard_tile``.
Deleting a dashboard cascades to groups and tiles and never touches a chart;
deleting a chart that is still placed is refused by ``ON DELETE RESTRICT``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base, DashboardRole, RefreshStatus, Visibility, ts_created, ts_updated, uuid_pk


def _enum(cls, name: str):
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class SavedChart(Base):
    """The durable artefact.  Valid with zero dashboards referencing it (the owner's library)."""

    __tablename__ = "saved_chart"
    __table_args__ = (
        Index("ix_saved_chart_owner_archived", "owner_id", "is_archived"),
        Index("ix_saved_chart_sql_hash", "sql_hash"),
        Index("ix_saved_chart_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_source.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    sql_text: Mapped[str] = mapped_column(Text, nullable=False)
    sql_hash: Mapped[str] = mapped_column(Text, nullable=False)
    chart_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    render_code: Mapped[str | None] = mapped_column(Text)  # re-executed on refresh, never by a model
    dataset_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="df")
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("turn.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = ts_created()
    updated_at: Mapped[datetime] = ts_updated()

    tiles: Mapped[list[DashboardTile]] = relationship(back_populates="chart", passive_deletes="all")


class Dashboard(Base):
    __tablename__ = "dashboard"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_dashboard_owner_name"),
        Index("ix_dashboard_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[Visibility] = mapped_column(
        _enum(Visibility, "dashboard_visibility"), nullable=False, server_default=Visibility.private.value
    )
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = ts_created()
    updated_at: Mapped[datetime] = ts_updated()

    groups: Mapped[list[DashboardGroup]] = relationship(
        back_populates="dashboard",
        cascade="all, delete-orphan",
        order_by="DashboardGroup.position",
        passive_deletes=True,
    )
    tiles: Mapped[list[DashboardTile]] = relationship(
        back_populates="dashboard", cascade="all, delete-orphan", passive_deletes=True, overlaps="group,tiles"
    )
    grants: Mapped[list[DashboardGrant]] = relationship(
        back_populates="dashboard", cascade="all, delete-orphan", passive_deletes=True
    )


class DashboardGroup(Base):
    __tablename__ = "dashboard_group"
    __table_args__ = (
        UniqueConstraint(
            "dashboard_id",
            "position",
            name="uq_dashboard_group_dashboard_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("char_length(title) <= 60", name="title_len"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dashboard.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(60), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_collapsed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = ts_created()

    dashboard: Mapped[Dashboard] = relationship(back_populates="groups")
    tiles: Mapped[list[DashboardTile]] = relationship(
        back_populates="group", order_by="DashboardTile.position", passive_deletes=True, overlaps="tiles,dashboard"
    )


class DashboardTile(Base):
    """The join table — the separation layer.  Overrides are presentation only, never SQL."""

    __tablename__ = "dashboard_tile"
    __table_args__ = (
        UniqueConstraint(
            "group_id", "position", name="uq_dashboard_tile_group_position", deferrable=True, initially="DEFERRED"
        ),
        Index("ix_dashboard_tile_dashboard", "dashboard_id"),
        Index("ix_dashboard_tile_chart", "saved_chart_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dashboard.id", ondelete="CASCADE"), nullable=False
    )  # denormalised for fast listing and for the grant check
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dashboard_group.id", ondelete="CASCADE"), nullable=False
    )
    saved_chart_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("saved_chart.id", ondelete="RESTRICT"), nullable=False
    )
    title_override: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    x: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    y: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    w: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("6"))
    h: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("4"))
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = ts_created()
    updated_at: Mapped[datetime] = ts_updated()

    dashboard: Mapped[Dashboard] = relationship(back_populates="tiles", overlaps="tiles")
    group: Mapped[DashboardGroup] = relationship(back_populates="tiles", overlaps="tiles,dashboard")
    chart: Mapped[SavedChart] = relationship(back_populates="tiles")


class DashboardGrant(Base):
    __tablename__ = "dashboard_grant"
    __table_args__ = (
        # exactly one owner per dashboard
        Index("uq_dashboard_grant_single_owner", "dashboard_id", unique=True, postgresql_where=text("role = 'owner'")),
        Index("ix_dashboard_grant_principal", "principal_id"),
    )

    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dashboard.id", ondelete="CASCADE"), primary_key=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[DashboardRole] = mapped_column(_enum(DashboardRole, "dashboard_role"), nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("app_user.id"))
    granted_at: Mapped[datetime] = ts_created()

    dashboard: Mapped[Dashboard] = relationship(back_populates="grants")


class TileRefresh(Base):
    __tablename__ = "tile_refresh"
    __table_args__ = (Index("ix_tile_refresh_tile_created", "tile_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dashboard_tile.id", ondelete="CASCADE"), nullable=False
    )
    viewer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )  # the scope applied is theirs
    status: Mapped[RefreshStatus] = mapped_column(_enum(RefreshStatus, "refresh_status"), nullable=False)
    row_count: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    sql_hash: Mapped[str | None] = mapped_column(Text)  # drift detection against the saved chart
    error_code: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts_created()
