"""tenant, app_user, refresh_token, data_source, data_source_view, access_scope, audit_log, usage_counter."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base, UserRole, ts_created, ts_updated, uuid_pk


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = ts_created()


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_app_user_tenant_email"),
        Index(
            "ix_app_user_external_subject",
            "external_subject",
            unique=True,
            postgresql_where=text("external_subject IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL for OIDC-provisioned users
    full_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=UserRole.viewer.value,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_subject: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts_created()
    updated_at: Mapped[datetime] = ts_updated()

    tenant: Mapped[Tenant] = relationship()


class RefreshToken(Base):
    __tablename__ = "refresh_token"
    __table_args__ = (
        Index("ix_refresh_token_user_revoked", "user_id", "revoked_at"),
        Index("ix_refresh_token_family", "family_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # sha256 of the opaque token
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    issued_at: Mapped[datetime] = ts_created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("refresh_token.id"))
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)


class DataSource(Base):
    __tablename__ = "data_source"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_data_source_tenant_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    dsn_secret_ref: Mapped[str] = mapped_column(Text, nullable=False)  # env var name, never a DSN
    read_only_role: Mapped[str] = mapped_column(Text, nullable=False)
    dialect: Mapped[str] = mapped_column(Text, nullable=False, server_default="postgresql")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = ts_created()
    updated_at: Mapped[datetime] = ts_updated()

    views: Mapped[list[DataSourceView]] = relationship(back_populates="data_source", cascade="all, delete-orphan")


class DataSourceView(Base):
    __tablename__ = "data_source_view"
    __table_args__ = (UniqueConstraint("data_source_id", "view_name", name="uq_data_source_view_source_view"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_source.id", ondelete="CASCADE"), nullable=False, index=True
    )
    view_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    business_rules: Mapped[str | None] = mapped_column(Text)
    column_metadata: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    scope_column: Mapped[str | None] = mapped_column(Text)
    allow_row_samples: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    data_source: Mapped[DataSource] = relationship(back_populates="views")


class AccessScope(Base):
    __tablename__ = "access_scope"
    __table_args__ = (
        UniqueConstraint("user_id", "data_source_id", "scope_key", name="uq_access_scope_user_source_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_source.id", ondelete="CASCADE"), nullable=False
    )
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    scope_values: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)  # ["*"] means unrestricted
    updated_at: Mapped[datetime] = ts_updated()


class UsageCounter(Base):
    __tablename__ = "usage_counter"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, server_default=text("0"))


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(40))
    object_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = ts_created()
