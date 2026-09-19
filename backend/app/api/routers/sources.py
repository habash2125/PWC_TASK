"""Data sources, the view allow-list, and access-scope assignment (admin write, analyst read)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, DbSession, request_meta, require_role
from app.api.errors import ConflictError, NotFoundError, ValidationFailed
from app.api.schemas import Out, Strict
from app.core.auth.rbac import Principal
from app.core.security.access_scope import DEFAULT_SCOPE_KEY
from app.db.models import DataSource, DataSourceView, UserRole
from app.db.repos.audit import AuditRepo
from app.db.repos.users import UserRepo

router = APIRouter(prefix="/sources", tags=["sources"])
Admin = Annotated[Principal, Depends(require_role(UserRole.admin))]


class SourceCreate(Strict):
    name: str = Field(min_length=1, max_length=120)
    dsn_secret_ref: str = Field(
        min_length=1, max_length=120, pattern=r"^[A-Z][A-Z0-9_]*$"
    )  # an env var NAME, never a DSN
    read_only_role: str = Field(min_length=1, max_length=120)
    dialect: str = Field(default="sqlite", max_length=40)


class SourceOut(Out):
    id: uuid.UUID
    name: str
    dsn_secret_ref: str
    read_only_role: str
    dialect: str
    is_active: bool
    created_at: datetime


class ViewUpdate(Strict):
    description: str | None = Field(default=None, max_length=2000)
    business_rules: str | None = Field(default=None, max_length=4000)
    is_enabled: bool | None = None
    allow_row_samples: bool | None = None
    scope_column: str | None = Field(default=None, max_length=80)


class ViewOut(Out):
    id: uuid.UUID
    view_name: str
    description: str
    business_rules: str | None
    column_metadata: list[dict[str, Any]]
    scope_column: str | None
    allow_row_samples: bool
    is_enabled: bool


class ScopeAssign(Strict):
    user_id: uuid.UUID
    scope_key: str = Field(default=DEFAULT_SCOPE_KEY, max_length=80)
    scope_values: list[int | str] = Field(max_length=500)


class ScopeOut(Out):
    user_id: uuid.UUID
    data_source_id: uuid.UUID
    scope_key: str
    scope_values: list


async def _source(session, source_id: uuid.UUID, tenant_id: uuid.UUID) -> DataSource:
    src = await session.get(DataSource, source_id)
    if src is None or src.tenant_id != tenant_id:
        raise NotFoundError("Data source not found")
    return src


@router.get("", response_model=list[SourceOut])
async def list_sources(session: DbSession, principal: CurrentPrincipal) -> list[SourceOut]:
    rows = (
        await session.execute(
            select(DataSource).where(DataSource.tenant_id == principal.tenant_id).order_by(DataSource.created_at)
        )
    ).scalars()
    return [SourceOut.model_validate(r) for r in rows]


@router.post("", response_model=SourceOut, status_code=201)
async def create_source(body: SourceCreate, session: DbSession, principal: Admin, request: Request) -> SourceOut:
    exists = (
        await session.execute(
            select(DataSource).where(DataSource.tenant_id == principal.tenant_id, DataSource.name == body.name)
        )
    ).first()
    if exists:
        raise ConflictError("A data source with that name exists")
    if "://" in body.dsn_secret_ref or "@" in body.dsn_secret_ref:
        raise ValidationFailed("dsn_secret_ref must be the name of a secret, never a DSN")
    src = DataSource(
        tenant_id=principal.tenant_id,
        name=body.name,
        dsn_secret_ref=body.dsn_secret_ref,
        read_only_role=body.read_only_role,
        dialect=body.dialect,
        created_by=principal.id,
    )
    session.add(src)
    await session.flush()
    await AuditRepo(session).write(
        action="source.create",
        tenant_id=principal.tenant_id,
        actor_user_id=principal.id,
        object_type="data_source",
        object_id=src.id,
        **request_meta(request),
    )
    return SourceOut.model_validate(src)


@router.get("/{source_id}/views", response_model=list[ViewOut])
async def list_views(source_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> list[ViewOut]:
    src = await _source(session, source_id, principal.tenant_id)
    rows = (
        await session.execute(
            select(DataSourceView).where(DataSourceView.data_source_id == src.id).order_by(DataSourceView.view_name)
        )
    ).scalars()
    out = []
    for r in rows:
        v = ViewOut.model_validate(r)
        if principal.role is not UserRole.admin:  # non-admins never see sensitive column names either
            v.column_metadata = [c for c in v.column_metadata if not c.get("sensitive")]
        out.append(v)
    return out


@router.patch("/{source_id}/views/{view_id}", response_model=ViewOut)
async def update_view(
    source_id: uuid.UUID, view_id: uuid.UUID, body: ViewUpdate, session: DbSession, principal: Admin, request: Request
) -> ViewOut:
    src = await _source(session, source_id, principal.tenant_id)
    row = await session.get(DataSourceView, view_id)
    if row is None or row.data_source_id != src.id:
        raise NotFoundError("View not found")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(row, k, v)
    await session.flush()
    await AuditRepo(session).write(
        action="allowlist.update",
        tenant_id=principal.tenant_id,
        actor_user_id=principal.id,
        object_type="data_source_view",
        object_id=row.id,
        metadata={"changes": list(changes)},
        **request_meta(request),
    )
    return ViewOut.model_validate(row)


@router.get("/{source_id}/scopes", response_model=list[ScopeOut])
async def list_scopes(source_id: uuid.UUID, session: DbSession, principal: Admin) -> list[ScopeOut]:
    src = await _source(session, source_id, principal.tenant_id)
    from app.db.models import AccessScope

    rows = (await session.execute(select(AccessScope).where(AccessScope.data_source_id == src.id))).scalars()
    return [ScopeOut.model_validate(r) for r in rows]


@router.put("/{source_id}/scopes", response_model=ScopeOut)
async def assign_scope(
    source_id: uuid.UUID, body: ScopeAssign, session: DbSession, principal: Admin, request: Request
) -> ScopeOut:
    src = await _source(session, source_id, principal.tenant_id)
    users = UserRepo(session)
    target = await users.by_id(body.user_id)
    if target is None or target.tenant_id != principal.tenant_id:
        raise NotFoundError("User not found")
    values: list = ["*"] if "*" in body.scope_values else [v for v in body.scope_values if v != "*"]
    row = await users.set_scope(target.id, src.id, body.scope_key, values)
    await AuditRepo(session).write(
        action="scope.assign",
        tenant_id=principal.tenant_id,
        actor_user_id=principal.id,
        object_type="access_scope",
        object_id=row.id,
        metadata={"user_id": str(target.id), "scope_key": body.scope_key, "values": values},
        **request_meta(request),
    )
    return ScopeOut.model_validate(row)
