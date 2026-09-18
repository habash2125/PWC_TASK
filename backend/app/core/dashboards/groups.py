"""Group (section) management and the dashboard read model."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DashboardContext
from app.api.errors import ConflictError, NotFoundError
from app.api.schemas.dashboards import DashboardOut, GroupCreate, GroupOut, GroupUpdate
from app.core.dashboards.tiles import tile_out
from app.db.models import Dashboard, DashboardGroup
from app.db.repos.dashboards import DashboardRepo


def dashboard_out(dashboard: Dashboard, ctx: DashboardContext) -> DashboardOut:
    groups = []
    for g in sorted(dashboard.groups, key=lambda x: x.position):
        tiles = [tile_out(t) for t in sorted(g.tiles, key=lambda t: t.position)]
        groups.append(GroupOut(id=g.id, title=g.title, position=g.position, is_collapsed=g.is_collapsed, tiles=tiles))
    return DashboardOut(
        id=dashboard.id,
        name=dashboard.name,
        description=dashboard.description,
        owner_id=dashboard.owner_id,
        visibility=dashboard.visibility,
        effective_role=ctx.effective_role,
        is_archived=dashboard.is_archived,
        groups=groups,
        created_at=dashboard.created_at,
        updated_at=dashboard.updated_at,
    )


async def add_group(session: AsyncSession, ctx: DashboardContext, body: GroupCreate) -> DashboardGroup:
    return await DashboardRepo(session).add_group(ctx.dashboard_id, body.title)


async def update_group(
    session: AsyncSession, ctx: DashboardContext, group_id: uuid.UUID, body: GroupUpdate
) -> DashboardGroup:
    repo = DashboardRepo(session)
    group = await repo.group(ctx.dashboard_id, group_id)
    if group is None:
        raise NotFoundError("Group not found")
    if body.title is not None:
        group.title = body.title
    if body.is_collapsed is not None:
        group.is_collapsed = body.is_collapsed
    if body.position is not None:
        groups = await repo.groups(ctx.dashboard_id)
        ids = [g.id for g in groups if g.id != group.id]
        ids.insert(min(body.position, len(ids)), group.id)
        await repo.reorder_groups(ctx.dashboard_id, ids)
    await session.flush()
    await session.refresh(group)
    return group


async def delete_group(session: AsyncSession, ctx: DashboardContext, group_id: uuid.UUID) -> int:
    repo = DashboardRepo(session)
    group = await repo.group(ctx.dashboard_id, group_id)
    if group is None:
        raise NotFoundError("Group not found")
    try:
        return await repo.delete_group(ctx.dashboard_id, group)
    except ValueError as exc:
        raise ConflictError(str(exc)) from None
