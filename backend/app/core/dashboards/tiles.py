"""Tile placement: presentation only.  A tile never carries SQL of its own."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DashboardContext
from app.api.errors import NotFoundError, ValidationFailed
from app.api.schemas.dashboards import LayoutUpdate, TileCreate, TileOut, TileUpdate
from app.db.models import DashboardTile
from app.db.repos.charts import ChartRepo
from app.db.repos.dashboards import DashboardRepo, TileGroupMismatch


def tile_out(tile: DashboardTile) -> TileOut:
    chart = tile.chart
    return TileOut(
        id=tile.id,
        dashboard_id=tile.dashboard_id,
        group_id=tile.group_id,
        saved_chart_id=tile.saved_chart_id,
        title=tile.title_override or chart.title,
        title_override=tile.title_override,
        question=chart.question,
        sql_text=chart.sql_text,
        sql_hash=chart.sql_hash,
        chart_spec=chart.chart_spec,
        chart_version=chart.version,
        position=tile.position,
        x=tile.x,
        y=tile.y,
        w=tile.w,
        h=tile.h,
        overrides=tile.overrides,
    )


async def add_tile(session: AsyncSession, ctx: DashboardContext, body: TileCreate) -> DashboardTile:
    repo = DashboardRepo(session)
    chart = await ChartRepo(session).get(body.saved_chart_id, tenant_id=ctx.principal.tenant_id)
    # the chart must be the caller's own, or already visible to them through another dashboard
    if chart is None or chart.is_archived:
        raise NotFoundError("Chart not found")
    if chart.owner_id != ctx.principal.id:
        from app.core.dashboards.charts import get_visible_chart

        await get_visible_chart(session, ctx.principal, chart.id)
    group_id = body.group_id or (await repo.default_group(ctx.dashboard_id)).id
    try:
        return await repo.add_tile(
            dashboard_id=ctx.dashboard_id,
            group_id=group_id,
            chart=chart,
            title_override=body.title_override,
            created_by=ctx.principal.id,
            w=body.w,
            h=body.h,
        )
    except TileGroupMismatch:
        raise NotFoundError("Group not found on this dashboard") from None


async def update_tile(
    session: AsyncSession, ctx: DashboardContext, tile_id: uuid.UUID, body: TileUpdate
) -> DashboardTile:
    repo = DashboardRepo(session)
    tile = await repo.tile(ctx.dashboard_id, tile_id)
    if tile is None:
        raise NotFoundError("Tile not found")
    if body.group_id is not None:
        try:
            await repo.move_tile(tile, group_id=body.group_id)
        except TileGroupMismatch:
            raise NotFoundError("Group not found on this dashboard") from None
    if "title_override" in body.model_fields_set:
        tile.title_override = body.title_override or None
    for key in ("x", "y", "w", "h"):
        value = getattr(body, key)
        if value is not None:
            setattr(tile, key, value)
    if body.overrides is not None:
        tile.overrides = body.overrides
    await session.flush()
    await session.refresh(tile, attribute_names=["chart"])
    return tile


async def delete_tile(session: AsyncSession, ctx: DashboardContext, tile_id: uuid.UUID) -> None:
    repo = DashboardRepo(session)
    tile = await repo.tile(ctx.dashboard_id, tile_id)
    if tile is None:
        raise NotFoundError("Tile not found")
    await repo.delete_tile(tile)  # the placement only; the saved_chart is untouched


async def apply_layout(session: AsyncSession, ctx: DashboardContext, body: LayoutUpdate) -> int:
    try:
        return await DashboardRepo(session).apply_layout(ctx.dashboard_id, [i.model_dump() for i in body.items])
    except TileGroupMismatch as exc:
        raise ValidationFailed(str(exc)) from None
