"""initial schema — every table in Section 4 of the build spec.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

ENUMS = (
    "user_role",
    "dashboard_role",
    "dashboard_visibility",
    "turn_status",
    "refresh_status",
    "guard_kind",
    "guard_verdict",
)


def upgrade() -> None:
    # citext gives case-insensitive emails; gen_random_uuid() is built in on PostgreSQL 13+
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.create_table(
        "tenant",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant")),
    )
    op.create_table(
        "trace_span",
        sa.Column("span_id", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("parent_span_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("start_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("span_id", name=op.f("pk_trace_span")),
    )
    op.create_index("ix_trace_span_start_ts", "trace_span", ["start_ts"], unique=False)
    op.create_index("ix_trace_span_trace_id", "trace_span", ["trace_id"], unique=False)
    op.create_table(
        "app_user",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("full_name", sa.Text(), nullable=True),
        sa.Column(
            "role", sa.Enum("viewer", "analyst", "admin", name="user_role"), server_default="viewer", nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_subject", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], name=op.f("fk_app_user_tenant_id_tenant")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("tenant_id", "email", name="uq_app_user_tenant_email"),
    )
    op.create_index(
        "ix_app_user_external_subject",
        "app_user",
        ["external_subject"],
        unique=True,
        postgresql_where=sa.text("external_subject IS NOT NULL"),
    )
    op.create_index(op.f("ix_app_user_tenant_id"), "app_user", ["tenant_id"], unique=False)
    op.create_table(
        "audit_log",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("object_type", sa.String(length=40), nullable=True),
        sa.Column("object_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["app_user.id"], name=op.f("fk_audit_log_actor_user_id_app_user"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], name=op.f("fk_audit_log_tenant_id_tenant")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_tenant_created", "audit_log", ["tenant_id", "created_at"], unique=False)
    op.create_table(
        "dashboard",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "visibility",
            sa.Enum("private", "shared", "tenant", name="dashboard_visibility"),
            server_default="private",
            nullable=False,
        ),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["app_user.id"], name=op.f("fk_dashboard_owner_id_app_user"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], name=op.f("fk_dashboard_tenant_id_tenant")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboard")),
        sa.UniqueConstraint("owner_id", "name", name="uq_dashboard_owner_name"),
    )
    op.create_index("ix_dashboard_tenant", "dashboard", ["tenant_id"], unique=False)
    op.create_table(
        "data_source",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("dsn_secret_ref", sa.Text(), nullable=False),
        sa.Column("read_only_role", sa.Text(), nullable=False),
        sa.Column("dialect", sa.Text(), server_default="postgresql", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"], name=op.f("fk_data_source_created_by_app_user")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], name=op.f("fk_data_source_tenant_id_tenant")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_source")),
        sa.UniqueConstraint("tenant_id", "name", name="uq_data_source_tenant_name"),
    )
    op.create_index(op.f("ix_data_source_tenant_id"), "data_source", ["tenant_id"], unique=False)
    op.create_table(
        "refresh_token",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.UUID(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"], ["refresh_token.id"], name=op.f("fk_refresh_token_replaced_by_id_refresh_token")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_refresh_token_user_id_app_user"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_token_token_hash")),
    )
    op.create_index("ix_refresh_token_family", "refresh_token", ["family_id"], unique=False)
    op.create_index("ix_refresh_token_user_revoked", "refresh_token", ["user_id", "revoked_at"], unique=False)
    op.create_table(
        "usage_counter",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("turn_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_usage_counter_user_id_app_user"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "day", name=op.f("pk_usage_counter")),
    )
    op.create_table(
        "access_scope",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("data_source_id", sa.UUID(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("scope_values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_source.id"],
            name=op.f("fk_access_scope_data_source_id_data_source"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_access_scope_user_id_app_user"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_scope")),
        sa.UniqueConstraint("user_id", "data_source_id", "scope_key", name="uq_access_scope_user_source_key"),
    )
    op.create_table(
        "chat_session",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("data_source_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["data_source_id"], ["data_source.id"], name=op.f("fk_chat_session_data_source_id_data_source")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_chat_session_user_id_app_user"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_session")),
    )
    op.create_index(op.f("ix_chat_session_user_id"), "chat_session", ["user_id"], unique=False)
    op.create_table(
        "dashboard_grant",
        sa.Column("dashboard_id", sa.UUID(), nullable=False),
        sa.Column("principal_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Enum("viewer", "editor", "owner", name="dashboard_role"), nullable=False),
        sa.Column("granted_by", sa.UUID(), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["dashboard_id"],
            ["dashboard.id"],
            name=op.f("fk_dashboard_grant_dashboard_id_dashboard"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["granted_by"], ["app_user.id"], name=op.f("fk_dashboard_grant_granted_by_app_user")),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["app_user.id"], name=op.f("fk_dashboard_grant_principal_id_app_user"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("dashboard_id", "principal_id", name=op.f("pk_dashboard_grant")),
    )
    op.create_index("ix_dashboard_grant_principal", "dashboard_grant", ["principal_id"], unique=False)
    op.create_index(
        "uq_dashboard_grant_single_owner",
        "dashboard_grant",
        ["dashboard_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner'"),
    )
    op.create_table(
        "dashboard_group",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("dashboard_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=60), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_collapsed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("char_length(title) <= 60", name=op.f("ck_dashboard_group_title_len")),
        sa.ForeignKeyConstraint(
            ["dashboard_id"],
            ["dashboard.id"],
            name=op.f("fk_dashboard_group_dashboard_id_dashboard"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboard_group")),
        sa.UniqueConstraint(
            "dashboard_id",
            "position",
            deferrable=True,
            initially="DEFERRED",
            name="uq_dashboard_group_dashboard_position",
        ),
    )
    op.create_index(op.f("ix_dashboard_group_dashboard_id"), "dashboard_group", ["dashboard_id"], unique=False)
    op.create_table(
        "data_source_view",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("data_source_id", sa.UUID(), nullable=False),
        sa.Column("view_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("business_rules", sa.Text(), nullable=True),
        sa.Column(
            "column_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("scope_column", sa.Text(), nullable=True),
        sa.Column("allow_row_samples", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_source.id"],
            name=op.f("fk_data_source_view_data_source_id_data_source"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_source_view")),
        sa.UniqueConstraint("data_source_id", "view_name", name="uq_data_source_view_source_view"),
    )
    op.create_index(op.f("ix_data_source_view_data_source_id"), "data_source_view", ["data_source_id"], unique=False)
    op.create_table(
        "turn",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer_markdown", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("ok", "blocked", "error", "timeout", name="turn_status"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("sql_text", sa.Text(), nullable=True),
        sa.Column("prompt_version_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("stage_timings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["chat_session.id"], name=op.f("fk_turn_session_id_chat_session"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], name=op.f("fk_turn_user_id_app_user")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_turn")),
    )
    op.create_index("ix_turn_session_created", "turn", ["session_id", "created_at"], unique=False)
    op.create_index("ix_turn_trace_id", "turn", ["trace_id"], unique=False)
    op.create_table(
        "feedback",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("rating IN (-1, 1)", name=op.f("ck_feedback_rating_sign")),
        sa.ForeignKeyConstraint(["turn_id"], ["turn.id"], name=op.f("fk_feedback_turn_id_turn"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_feedback_user_id_app_user"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
    )
    op.create_index(op.f("ix_feedback_turn_id"), "feedback", ["turn_id"], unique=False)
    op.create_table(
        "saved_chart",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("data_source_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("sql_text", sa.Text(), nullable=False),
        sa.Column("sql_hash", sa.Text(), nullable=False),
        sa.Column("chart_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "params", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("source_turn_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_source.id"],
            name=op.f("fk_saved_chart_data_source_id_data_source"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["app_user.id"], name=op.f("fk_saved_chart_owner_id_app_user"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_turn_id"], ["turn.id"], name=op.f("fk_saved_chart_source_turn_id_turn"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], name=op.f("fk_saved_chart_tenant_id_tenant")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_chart")),
    )
    op.create_index("ix_saved_chart_owner_archived", "saved_chart", ["owner_id", "is_archived"], unique=False)
    op.create_index("ix_saved_chart_sql_hash", "saved_chart", ["sql_hash"], unique=False)
    op.create_index("ix_saved_chart_tenant", "saved_chart", ["tenant_id"], unique=False)
    op.create_table(
        "turn_chart",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("chart_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sql_text", sa.Text(), nullable=False),
        sa.Column("sql_hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["turn_id"], ["turn.id"], name=op.f("fk_turn_chart_turn_id_turn"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_turn_chart")),
        sa.UniqueConstraint("turn_id", "position", name="uq_turn_chart_turn_position"),
    )
    op.create_table(
        "dashboard_tile",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("dashboard_id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("saved_chart_id", sa.UUID(), nullable=False),
        sa.Column("title_override", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("x", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("y", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("w", sa.Integer(), server_default=sa.text("6"), nullable=False),
        sa.Column("h", sa.Integer(), server_default=sa.text("4"), nullable=False),
        sa.Column(
            "overrides", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"], name=op.f("fk_dashboard_tile_created_by_app_user")),
        sa.ForeignKeyConstraint(
            ["dashboard_id"],
            ["dashboard.id"],
            name=op.f("fk_dashboard_tile_dashboard_id_dashboard"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["dashboard_group.id"],
            name=op.f("fk_dashboard_tile_group_id_dashboard_group"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["saved_chart_id"],
            ["saved_chart.id"],
            name=op.f("fk_dashboard_tile_saved_chart_id_saved_chart"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dashboard_tile")),
        sa.UniqueConstraint(
            "group_id", "position", deferrable=True, initially="DEFERRED", name="uq_dashboard_tile_group_position"
        ),
    )
    op.create_index("ix_dashboard_tile_chart", "dashboard_tile", ["saved_chart_id"], unique=False)
    op.create_index("ix_dashboard_tile_dashboard", "dashboard_tile", ["dashboard_id"], unique=False)
    op.create_table(
        "tile_refresh",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tile_id", sa.UUID(), nullable=False),
        sa.Column("viewer_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Enum("ok", "blocked", "invalid_query", "error", name="refresh_status"), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("sql_hash", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tile_id"], ["dashboard_tile.id"], name=op.f("fk_tile_refresh_tile_id_dashboard_tile"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["viewer_id"], ["app_user.id"], name=op.f("fk_tile_refresh_viewer_id_app_user")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tile_refresh")),
    )
    op.create_index("ix_tile_refresh_tile_created", "tile_refresh", ["tile_id", "created_at"], unique=False)
    op.create_table(
        "guard_event",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=True),
        sa.Column("tile_refresh_id", sa.UUID(), nullable=True),
        sa.Column(
            "kind", sa.Enum("injection", "sql_guard", "scope", "budget", "loop", name="guard_kind"), nullable=False
        ),
        sa.Column("verdict", sa.Enum("allowed", "repaired", "blocked", name="guard_verdict"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("offending_sql", sa.Text(), nullable=True),
        sa.Column("shadow_parser_verdict", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tile_refresh_id"],
            ["tile_refresh.id"],
            name=op.f("fk_guard_event_tile_refresh_id_tile_refresh"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["turn.id"], name=op.f("fk_guard_event_turn_id_turn"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guard_event")),
    )
    op.create_index("ix_guard_event_created", "guard_event", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_guard_event_created", table_name="guard_event")
    op.drop_table("guard_event")
    op.drop_index("ix_tile_refresh_tile_created", table_name="tile_refresh")
    op.drop_table("tile_refresh")
    op.drop_index("ix_dashboard_tile_dashboard", table_name="dashboard_tile")
    op.drop_index("ix_dashboard_tile_chart", table_name="dashboard_tile")
    op.drop_table("dashboard_tile")
    op.drop_table("turn_chart")
    op.drop_index("ix_saved_chart_tenant", table_name="saved_chart")
    op.drop_index("ix_saved_chart_sql_hash", table_name="saved_chart")
    op.drop_index("ix_saved_chart_owner_archived", table_name="saved_chart")
    op.drop_table("saved_chart")
    op.drop_index(op.f("ix_feedback_turn_id"), table_name="feedback")
    op.drop_table("feedback")
    op.drop_index("ix_turn_trace_id", table_name="turn")
    op.drop_index("ix_turn_session_created", table_name="turn")
    op.drop_table("turn")
    op.drop_index(op.f("ix_data_source_view_data_source_id"), table_name="data_source_view")
    op.drop_table("data_source_view")
    op.drop_index(op.f("ix_dashboard_group_dashboard_id"), table_name="dashboard_group")
    op.drop_table("dashboard_group")
    op.drop_index(
        "uq_dashboard_grant_single_owner", table_name="dashboard_grant", postgresql_where=sa.text("role = 'owner'")
    )
    op.drop_index("ix_dashboard_grant_principal", table_name="dashboard_grant")
    op.drop_table("dashboard_grant")
    op.drop_index(op.f("ix_chat_session_user_id"), table_name="chat_session")
    op.drop_table("chat_session")
    op.drop_table("access_scope")
    op.drop_table("usage_counter")
    op.drop_index("ix_refresh_token_user_revoked", table_name="refresh_token")
    op.drop_index("ix_refresh_token_family", table_name="refresh_token")
    op.drop_table("refresh_token")
    op.drop_index(op.f("ix_data_source_tenant_id"), table_name="data_source")
    op.drop_table("data_source")
    op.drop_index("ix_dashboard_tenant", table_name="dashboard")
    op.drop_table("dashboard")
    op.drop_index("ix_audit_log_tenant_created", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index(op.f("ix_app_user_tenant_id"), table_name="app_user")
    op.drop_index(
        "ix_app_user_external_subject", table_name="app_user", postgresql_where=sa.text("external_subject IS NOT NULL")
    )
    op.drop_table("app_user")
    op.drop_index("ix_trace_span_trace_id", table_name="trace_span")
    op.drop_index("ix_trace_span_start_ts", table_name="trace_span")
    op.drop_table("trace_span")
    op.drop_table("tenant")
    for enum_name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
