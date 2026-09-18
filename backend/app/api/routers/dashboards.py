"""Dashboards: container → groups → tiles, with per-dashboard grants.

Every route below resolves its authorisation through ``require_dashboard_role``;
nothing in a route body decides who may do what.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import (
    CurrentPrincipal,
    DashboardContext,
    DbSession,
    IdempotencyDep,
    rate_limit,
    request_meta,
    require_dashboard_role,
    require_role,
)
from app.api.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailed
from app.api.schemas.auth import MessageOut
from app.api.schemas.dashboards import (
    ApplyGroupingOut,
    ApplyGroupingRequest,
    DashboardCreate,
    DashboardOut,
    DashboardRefreshOut,
    DashboardSummaryOut,
    DashboardUpdate,
    GrantOut,
    GrantsUpdate,
    GroupCreate,
    GroupingProposalOut,
    GroupOut,
    GroupUpdate,
    LayoutUpdate,
    TileCreate,
    TileOut,
    TileRefreshOut,
    TileUpdate,
)
from app.core.auth.rbac import Principal, effective_dashboard_role
from app.core.dashboards import groups as group_service
from app.core.dashboards import tiles as tile_service
from app.core.dashboards.groups import dashboard_out
from app.core.dashboards.tiles import tile_out
from app.db.models import DashboardRole, UserRole
from app.db.repos.audit import AuditRepo
from app.db.repos.dashboards import DashboardRepo
from app.db.repos.users import UserRepo

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

Viewer = Annotated[DashboardContext, Depends(require_dashboard_role(DashboardRole.viewer))]
Editor = Annotated[DashboardContext, Depends(require_dashboard_role(DashboardRole.editor))]
Owner = Annotated[DashboardContext, Depends(require_dashboard_role(DashboardRole.owner))]
Analyst = Annotated[Principal, Depends(require_role(UserRole.analyst))]


# ── dashboards ───────────────────────────────────────────────────────────────


@router.get("", response_model=list[DashboardSummaryOut])
async def list_dashboards(principal: CurrentPrincipal, session: DbSession) -> list[DashboardSummaryOut]:
    repo = DashboardRepo(session)
    out = []
    for dashboard, grant in await repo.list_visible(principal.id, principal.tenant_id):
        effective = effective_dashboard_role(principal.role, grant or DashboardRole.viewer)
        assert effective is not None
        tiles = await repo.tiles(dashboard.id)
        out.append(
            DashboardSummaryOut(
                id=dashboard.id,
                name=dashboard.name,
                description=dashboard.description,
                owner_id=dashboard.owner_id,
                visibility=dashboard.visibility,
                effective_role=effective,
                tile_count=len(tiles),
                updated_at=dashboard.updated_at,
            )
        )
    return out


@router.post("", response_model=DashboardOut, status_code=201)
async def create_dashboard(
    body: DashboardCreate, principal: Analyst, session: DbSession, request: Request
) -> DashboardOut:
    repo = DashboardRepo(session)
    if await repo.name_taken(principal.id, body.name):
        raise ConflictError("You already have a dashboard with that name")
    dashboard = await repo.create(
        tenant_id=principal.tenant_id, owner_id=principal.id, name=body.name, description=body.description
    )
    await AuditRepo(session).write(
        action="dashboard.create",
        tenant_id=principal.tenant_id,
        actor_user_id=principal.id,
        object_type="dashboard",
        object_id=dashboard.id,
        **request_meta(request),
    )
    full = await repo.get_full(dashboard.id)
    ctx = DashboardContext(
        dashboard_id=dashboard.id, principal=principal, effective_role=DashboardRole.owner, owner_id=principal.id
    )
    return dashboard_out(full, ctx)


@router.get("/{dashboard_id}", response_model=DashboardOut)
async def get_dashboard(ctx: Viewer, session: DbSession) -> DashboardOut:
    full = await DashboardRepo(session).get_full(ctx.dashboard_id)
    if full is None:
        raise NotFoundError("Dashboard not found")
    return dashboard_out(full, ctx)


@router.patch("/{dashboard_id}", response_model=DashboardOut)
async def update_dashboard(ctx: Editor, body: DashboardUpdate, session: DbSession, request: Request) -> DashboardOut:
    repo = DashboardRepo(session)
    dashboard = await repo.get(ctx.dashboard_id, tenant_id=ctx.principal.tenant_id)
    assert dashboard is not None
    if (body.visibility is not None or body.is_archived is not None) and ctx.effective_role is not DashboardRole.owner:
        raise ForbiddenError("Only the owner can change visibility or archive a dashboard")
    if body.name is not None and body.name != dashboard.name:
        if await repo.name_taken(dashboard.owner_id, body.name, exclude=dashboard.id):
            raise ConflictError("The owner already has a dashboard with that name")
        dashboard.name = body.name
    if body.description is not None:
        dashboard.description = body.description
    if body.visibility is not None:
        dashboard.visibility = body.visibility
    if body.is_archived is not None:
        dashboard.is_archived = body.is_archived
    await session.flush()
    await AuditRepo(session).write(
        action="dashboard.update",
        tenant_id=ctx.principal.tenant_id,
        actor_user_id=ctx.principal.id,
        object_type="dashboard",
        object_id=dashboard.id,
        **request_meta(request),
    )
    full = await repo.get_full(dashboard.id)
    return dashboard_out(full, ctx)


@router.delete("/{dashboard_id}", status_code=204, response_model=None)
async def delete_dashboard(ctx: Owner, session: DbSession, request: Request) -> None:
    """Owner only.  Cascades groups, tiles and grants; never touches a saved chart."""
    repo = DashboardRepo(session)
    dashboard = await repo.get(ctx.dashboard_id, tenant_id=ctx.principal.tenant_id)
    assert dashboard is not None
    await repo.delete(dashboard)
    await AuditRepo(session).write(
        action="dashboard.delete",
        tenant_id=ctx.principal.tenant_id,
        actor_user_id=ctx.principal.id,
        object_type="dashboard",
        object_id=ctx.dashboard_id,
        **request_meta(request),
    )


# ── groups ───────────────────────────────────────────────────────────────────


@router.post("/{dashboard_id}/groups", response_model=GroupOut, status_code=201)
async def create_group(ctx: Editor, body: GroupCreate, session: DbSession) -> GroupOut:
    g = await group_service.add_group(session, ctx, body)
    return GroupOut(id=g.id, title=g.title, position=g.position, is_collapsed=g.is_collapsed, tiles=[])


@router.patch("/{dashboard_id}/groups/{group_id}", response_model=GroupOut)
async def update_group(ctx: Editor, group_id: uuid.UUID, body: GroupUpdate, session: DbSession) -> GroupOut:
    g = await group_service.update_group(session, ctx, group_id, body)
    tiles = [tile_out(t) for t in await DashboardRepo(session).tiles(ctx.dashboard_id) if t.group_id == g.id]
    return GroupOut(id=g.id, title=g.title, position=g.position, is_collapsed=g.is_collapsed, tiles=tiles)


@router.delete("/{dashboard_id}/groups/{group_id}", response_model=MessageOut)
async def delete_group(ctx: Editor, group_id: uuid.UUID, session: DbSession) -> MessageOut:
    moved = await group_service.delete_group(session, ctx, group_id)
    return MessageOut(message=f"group removed; {moved} tile(s) moved to the default group")


# ── tiles ────────────────────────────────────────────────────────────────────


@router.post("/{dashboard_id}/tiles", response_model=TileOut, status_code=201)
async def create_tile(ctx: Editor, body: TileCreate, session: DbSession, request: Request) -> TileOut:
    tile = await tile_service.add_tile(session, ctx, body)
    await AuditRepo(session).write(
        action="tile.create",
        tenant_id=ctx.principal.tenant_id,
        actor_user_id=ctx.principal.id,
        object_type="dashboard_tile",
        object_id=tile.id,
        metadata={"dashboard_id": str(ctx.dashboard_id), "chart_id": str(tile.saved_chart_id)},
        **request_meta(request),
    )
    return tile_out(tile)


@router.patch("/{dashboard_id}/tiles/{tile_id}", response_model=TileOut)
async def update_tile(ctx: Editor, tile_id: uuid.UUID, body: TileUpdate, session: DbSession) -> TileOut:
    return tile_out(await tile_service.update_tile(session, ctx, tile_id, body))


@router.delete("/{dashboard_id}/tiles/{tile_id}", status_code=204, response_model=None)
async def delete_tile(ctx: Editor, tile_id: uuid.UUID, session: DbSession) -> None:
    await tile_service.delete_tile(session, ctx, tile_id)


@router.put("/{dashboard_id}/layout", response_model=DashboardOut)
async def put_layout(ctx: Editor, body: LayoutUpdate, session: DbSession) -> DashboardOut:
    """Bulk reorder after a drag; one transaction."""
    await tile_service.apply_layout(session, ctx, body)
    full = await DashboardRepo(session).get_full(ctx.dashboard_id)
    return dashboard_out(full, ctx)


# ── refresh (zero LLM calls) ─────────────────────────────────────────────────


@router.post(
    "/{dashboard_id}/tiles/{tile_id}/refresh",
    response_model=TileRefreshOut,
    dependencies=[Depends(rate_limit("refresh"))],
)
async def refresh_tile(ctx: Viewer, tile_id: uuid.UUID, session: DbSession, idem: IdempotencyDep):
    from app.core.dashboards.refresh import refresh_tile as do_refresh

    if (replay := await idem.replay()) is not None:
        return replay
    tile = await DashboardRepo(session).tile(ctx.dashboard_id, tile_id)
    if tile is None:
        raise NotFoundError("Tile not found")
    out = await do_refresh(session, ctx.principal, tile)
    await idem.store(200, out.model_dump(mode="json"))
    return out


@router.post(
    "/{dashboard_id}/refresh", response_model=DashboardRefreshOut, dependencies=[Depends(rate_limit("refresh"))]
)
async def refresh_dashboard(ctx: Viewer, session: DbSession) -> DashboardRefreshOut:
    from app.core.dashboards.refresh import refresh_dashboard as do_refresh

    return await do_refresh(session, ctx.principal, ctx.dashboard_id)


# ── grants ───────────────────────────────────────────────────────────────────


@router.get("/{dashboard_id}/grants", response_model=list[GrantOut])
async def list_grants(ctx: Viewer, session: DbSession) -> list[GrantOut]:
    repo = DashboardRepo(session)
    users = {u.id: u for u in await UserRepo(session).list_in_tenant(ctx.principal.tenant_id)}
    out = []
    for g in await repo.grants(ctx.dashboard_id):
        u = users.get(g.principal_id)
        out.append(
            GrantOut(
                principal_id=g.principal_id,
                email=u.email if u else None,
                full_name=u.full_name if u else None,
                role=g.role,
                granted_at=g.granted_at,
            )
        )
    return out


@router.put("/{dashboard_id}/grants", response_model=list[GrantOut])
async def put_grants(ctx: Owner, body: GrantsUpdate, session: DbSession, request: Request) -> list[GrantOut]:
    """Owner only.  Replaces the grant set; exactly one owner must remain (ownership transfers with it)."""
    repo = DashboardRepo(session)
    dashboard = await repo.get(ctx.dashboard_id, tenant_id=ctx.principal.tenant_id)
    assert dashboard is not None
    users = {u.id: u for u in await UserRepo(session).list_in_tenant(ctx.principal.tenant_id)}
    grants: dict[uuid.UUID, DashboardRole] = {}
    for item in body.grants:
        if item.principal_id not in users:
            raise ValidationFailed("Every principal must be a user in your tenant")
        grants[item.principal_id] = item.role
    before = {g.principal_id for g in await repo.grants(ctx.dashboard_id)}
    try:
        await repo.replace_grants(dashboard, grants, granted_by=ctx.principal.id)
    except ValueError as exc:
        raise ConflictError(str(exc)) from None
    after = set(grants)
    audit = AuditRepo(session)
    meta = request_meta(request)
    for pid in after - before:
        await audit.write(
            action="dashboard.share",
            tenant_id=ctx.principal.tenant_id,
            actor_user_id=ctx.principal.id,
            object_type="dashboard",
            object_id=dashboard.id,
            metadata={"principal_id": str(pid), "role": grants[pid].value},
            **meta,
        )
    for pid in before - after:
        await audit.write(
            action="dashboard.unshare",
            tenant_id=ctx.principal.tenant_id,
            actor_user_id=ctx.principal.id,
            object_type="dashboard",
            object_id=dashboard.id,
            metadata={"principal_id": str(pid)},
            **meta,
        )
    return await list_grants(ctx, session)


# ── LLM-assisted grouping: model proposes, schema validates, code applies, user owns ──


@router.post(
    "/{dashboard_id}/suggest-groups", response_model=GroupingProposalOut, dependencies=[Depends(rate_limit("chat"))]
)
async def suggest_groups(ctx: Editor, session: DbSession) -> GroupingProposalOut:
    from app.core.dashboards.grouping_ai import suggest_grouping

    return await suggest_grouping(session, ctx)


@router.post("/{dashboard_id}/apply-grouping", response_model=ApplyGroupingOut)
async def apply_grouping(
    ctx: Editor, body: ApplyGroupingRequest, session: DbSession, request: Request
) -> ApplyGroupingOut:
    from app.core.dashboards.grouping_ai import apply_grouping as do_apply

    out = await do_apply(session, ctx, body)
    await AuditRepo(session).write(
        action="dashboard.apply_grouping",
        tenant_id=ctx.principal.tenant_id,
        actor_user_id=ctx.principal.id,
        object_type="dashboard",
        object_id=ctx.dashboard_id,
        metadata=out.model_dump(mode="json"),
        **request_meta(request),
    )
    return out
