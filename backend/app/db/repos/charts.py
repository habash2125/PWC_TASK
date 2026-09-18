from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Dashboard, DashboardGroup, DashboardTile, SavedChart


class ChartRepo:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def get(self, chart_id: uuid.UUID, *, tenant_id: uuid.UUID) -> SavedChart | None:
        chart = await self.s.get(SavedChart, chart_id)
        if chart is None or chart.tenant_id != tenant_id:
            return None
        return chart

    async def list_for_owner(
        self, owner_id: uuid.UUID, *, include_archived: bool, search: str | None, limit: int, offset: int
    ) -> tuple[list[SavedChart], int]:
        q = select(SavedChart).where(SavedChart.owner_id == owner_id)
        if not include_archived:
            q = q.where(SavedChart.is_archived.is_(False))
        if search:
            pattern = f"%{search}%"
            q = q.where(SavedChart.title.ilike(pattern) | SavedChart.question.ilike(pattern))
        total = (await self.s.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
        rows = (
            (await self.s.execute(q.order_by(SavedChart.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
        )
        return list(rows), int(total)

    async def create(self, **fields: Any) -> SavedChart:
        chart = SavedChart(**fields)
        self.s.add(chart)
        await self.s.flush()
        return chart

    async def usage(self, chart_id: uuid.UUID) -> list[dict[str, Any]]:
        """Every placement of the chart: dashboard, group and tile.  One query, ordered for display."""
        q = (
            select(
                Dashboard.id.label("dashboard_id"),
                Dashboard.name.label("dashboard_name"),
                Dashboard.owner_id,
                DashboardGroup.id.label("group_id"),
                DashboardGroup.title.label("group_title"),
                DashboardTile.id.label("tile_id"),
                DashboardTile.title_override,
            )
            .join(DashboardGroup, DashboardGroup.id == DashboardTile.group_id)
            .join(Dashboard, Dashboard.id == DashboardTile.dashboard_id)
            .where(DashboardTile.saved_chart_id == chart_id)
            .order_by(Dashboard.name, DashboardGroup.position, DashboardTile.position)
        )
        return [dict(r._mapping) for r in (await self.s.execute(q)).all()]

    async def placement_count(self, chart_id: uuid.UUID) -> int:
        return int(
            (
                await self.s.execute(
                    select(func.count()).select_from(DashboardTile).where(DashboardTile.saved_chart_id == chart_id)
                )
            ).scalar_one()
        )

    async def by_sql_hash(self, sql_hash: str, *, tenant_id: uuid.UUID) -> list[SavedChart]:
        q = select(SavedChart).where(SavedChart.sql_hash == sql_hash, SavedChart.tenant_id == tenant_id)
        return list((await self.s.execute(q)).scalars())

    async def delete(self, chart: SavedChart) -> None:
        await self.s.delete(chart)
        await self.s.flush()
