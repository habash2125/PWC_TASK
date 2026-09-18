from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    # server-generated ids/timestamps are fetched with RETURNING, so a freshly flushed row never
    # needs a lazy refresh from inside a Pydantic validator (which cannot await)
    __mapper_args__ = {"eager_defaults": True}


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def ts_created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def ts_updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class UserRole(enum.StrEnum):
    viewer = "viewer"
    analyst = "analyst"
    admin = "admin"


class DashboardRole(enum.StrEnum):
    viewer = "viewer"
    editor = "editor"
    owner = "owner"


class Visibility(enum.StrEnum):
    private = "private"
    shared = "shared"
    tenant = "tenant"


class TurnStatus(enum.StrEnum):
    ok = "ok"
    blocked = "blocked"
    error = "error"
    timeout = "timeout"


class RefreshStatus(enum.StrEnum):
    ok = "ok"
    blocked = "blocked"
    invalid_query = "invalid_query"
    error = "error"


class GuardKind(enum.StrEnum):
    injection = "injection"
    sql_guard = "sql_guard"
    scope = "scope"
    budget = "budget"
    loop = "loop"


class GuardVerdict(enum.StrEnum):
    allowed = "allowed"
    repaired = "repaired"
    blocked = "blocked"
