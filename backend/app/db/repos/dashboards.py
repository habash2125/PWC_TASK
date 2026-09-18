"""Dashboard, group, tile and grant persistence.

Repository-layer invariants (the database cannot express them as constraints):

* a tile's ``group_id`` must belong to its ``dashboard_id`` (``TileGroupMismatch``);
* a dashboard always has exactly one owner grant (the partial unique index holds the
  upper bound; ``replace_grants`` holds the lower bound);
* deleting a group never orphans a tile — tiles move to the default group.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Dashboard,
    DashboardGrant,
    DashboardGroup,
    DashboardRole,
    DashboardTile,
    SavedChart,
    TileRefresh,
    Visibility,
)

DEFAULT_GROUP_TITLE = "Overview"


class TileGroupMismatch(Exception):
    """Raised when a tile would be placed into a group that belongs to another dashboard."""


class DashboardRepo:
    def __init__(self, session: AsyncSession):
        self.s = session

    # ── dashboards ───────────────────────────────────────────────────────────
    async def get(self, dashboard_id: uuid.UUID, *, tenant_id: uuid.UUID) -> Dashboard | None:
        d = await self.s.get(Dashboard, dashboard_id)
        if d is None or d.tenant_id != tenant_id:
            return None
        return d

    async def get_full(self, dashboard_id: uuid.UUID) -> Dashboard | None:
        """Dashboard with groups, tiles and chart specs in one round trip."""
        q = (
            select(Dashboard)
            .where(Dashboard.id == dashboard_id)
            .options(
                selectinload(Dashboard.groups).selectinload(DashboardGroup.tiles).selectinload(DashboardTile.chart),
                selectinload(Dashboard.grants),
            )
        )
        return (await self.s.execute(q)).scalar_one_or_none()

    async def list_visible(
        self, principal_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> list[tuple[Dashboard, DashboardRole | None]]:
        """Owned + granted + tenant-visible, each with the *grant* role (the caller applies the ceiling)."""
        q = (
            select(Dashboard, DashboardGrant.role)
            .outerjoin(
                DashboardGrant,
                (DashboardGrant.dashboard_id == Dashboard.id) & (DashboardGrant.principal_id == principal_id),
            )
            .where(Dashboard.tenant_id == tenant_id, Dashboard.is_archived.is_(False))
            .where((DashboardGrant.principal_id.is_not(None)) | (Dashboard.visibility == Visibility.tenant))
            .order_by(Dashboard.updated_at.desc())
        )
        rows = (await self.s.execute(q)).all()
        return [(d, r) for d, r in rows]

    async def create(
        self, *, tenant_id: uuid.UUID, owner_id: uuid.UUID, name: str, description: str | None
    ) -> Dashboard:
        d = Dashboard(tenant_id=tenant_id, owner_id=owner_id, name=name, description=description)
        self.s.add(d)
        await self.s.flush()
        self.s.add(DashboardGroup(dashboard_id=d.id, title=DEFAULT_GROUP_TITLE, position=0))
        self.s.add(
            DashboardGrant(dashboard_id=d.id, principal_id=owner_id, role=DashboardRole.owner, granted_by=owner_id)
        )
        await self.s.flush()
        return d

    async def name_taken(self, owner_id: uuid.UUID, name: str, exclude: uuid.UUID | None = None) -> bool:
        q = select(func.count()).select_from(Dashboard).where(Dashboard.owner_id == owner_id, Dashboard.name == name)
        if exclude is not None:
            q = q.where(Dashboard.id != exclude)
        return int((await self.s.execute(q)).scalar_one()) > 0

    async def delete(self, dashboard: Dashboard) -> None:
        # ON DELETE CASCADE removes groups, tiles and grants; saved_chart rows are untouched
        await self.s.execute(delete(Dashboard).where(Dashboard.id == dashboard.id))
        await self.s.flush()

    # ── grants ───────────────────────────────────────────────────────────────
    async def grant_for(self, dashboard_id: uuid.UUID, principal_id: uuid.UUID) -> DashboardRole | None:
        q = select(DashboardGrant.role).where(
            DashboardGrant.dashboard_id == dashboard_id, DashboardGrant.principal_id == principal_id
        )
        return (await self.s.execute(q)).scalar_one_or_none()

    async def grants(self, dashboard_id: uuid.UUID) -> list[DashboardGrant]:
        q = (
            select(DashboardGrant)
            .where(DashboardGrant.dashboard_id == dashboard_id)
            .order_by(DashboardGrant.granted_at)
        )
        return list((await self.s.execute(q)).scalars())

    async def replace_grants(
        self, dashboard: Dashboard, grants: dict[uuid.UUID, DashboardRole], granted_by: uuid.UUID
    ) -> None:
        """Atomically replaces the grant set.  Exactly one owner must remain; ownership transfers with it."""
        owners = [pid for pid, role in grants.items() if role is DashboardRole.owner]
        if len(owners) != 1:
            raise ValueError("a dashboard must have exactly one owner")
        # delete-then-insert inside the caller's transaction; the partial unique index is the upper bound
        await self.s.execute(delete(DashboardGrant).where(DashboardGrant.dashboard_id == dashboard.id))
        for pid, role in grants.items():
            self.s.add(DashboardGrant(dashboard_id=dashboard.id, principal_id=pid, role=role, granted_by=granted_by))
        dashboard.owner_id = owners[0]
        if dashboard.visibility is Visibility.private and len(grants) > 1:
            dashboard.visibility = Visibility.shared
        await self.s.flush()

    # ── groups ───────────────────────────────────────────────────────────────
    async def groups(self, dashboard_id: uuid.UUID) -> list[DashboardGroup]:
        q = select(DashboardGroup).where(DashboardGroup.dashboard_id == dashboard_id).order_by(DashboardGroup.position)
        return list((await self.s.execute(q)).scalars())

    async def group(self, dashboard_id: uuid.UUID, group_id: uuid.UUID) -> DashboardGroup | None:
        g = await self.s.get(DashboardGroup, group_id)
        if g is None or g.dashboard_id != dashboard_id:
            return None
        return g

    async def default_group(self, dashboard_id: uuid.UUID) -> DashboardGroup:
        groups = await self.groups(dashboard_id)
        if not groups:  # cannot normally happen; every dashboard is born with one
            g = DashboardGroup(dashboard_id=dashboard_id, title=DEFAULT_GROUP_TITLE, position=0)
            self.s.add(g)
            await self.s.flush()
            return g
        return groups[0]

    async def add_group(self, dashboard_id: uuid.UUID, title: str) -> DashboardGroup:
        position = (
            int(
                (
                    await self.s.execute(
                        select(func.coalesce(func.max(DashboardGroup.position), -1)).where(
                            DashboardGroup.dashboard_id == dashboard_id
                        )
                    )
                ).scalar_one()
            )
            + 1
        )
        g = DashboardGroup(dashboard_id=dashboard_id, title=title, position=position)
        self.s.add(g)
        await self.s.flush()
        return g

    async def reorder_groups(self, dashboard_id: uuid.UUID, ordered_ids: list[uuid.UUID]) -> None:
        # the unique (dashboard_id, position) constraint is DEFERRABLE INITIALLY DEFERRED, so swaps are safe
        for pos, gid in enumerate(ordered_ids):
            await self.s.execute(
                update(DashboardGroup)
                .where(DashboardGroup.id == gid, DashboardGroup.dashboard_id == dashboard_id)
                .values(position=pos)
            )
        await self.s.flush()

    async def delete_group(self, dashboard_id: uuid.UUID, group: DashboardGroup) -> int:
        """Tiles move to the default group (appended), never orphaned.  Returns the moved count."""
        target = await self.default_group(dashboard_id)
        if target.id == group.id:
            raise ValueError("the default group cannot be deleted")
        next_pos = (
            int(
                (
                    await self.s.execute(
                        select(func.coalesce(func.max(DashboardTile.position), -1)).where(
                            DashboardTile.group_id == target.id
                        )
                    )
                ).scalar_one()
            )
            + 1
        )
        tiles = list(
            (
                await self.s.execute(
                    select(DashboardTile).where(DashboardTile.group_id == group.id).order_by(DashboardTile.position)
                )
            ).scalars()
        )
        for offset, tile in enumerate(tiles):
            tile.group_id = target.id
            tile.position = next_pos + offset
        await self.s.flush()
        await self.s.execute(delete(DashboardGroup).where(DashboardGroup.id == group.id))
        remaining = await self.groups(dashboard_id)
        await self.reorder_groups(dashboard_id, [g.id for g in remaining])
        return len(tiles)

    # ── tiles ────────────────────────────────────────────────────────────────
    async def tile(self, dashboard_id: uuid.UUID, tile_id: uuid.UUID) -> DashboardTile | None:
        t = await self.s.get(DashboardTile, tile_id, options=[selectinload(DashboardTile.chart)])
        if t is None or t.dashboard_id != dashboard_id:
            return None
        return t

    async def tiles(self, dashboard_id: uuid.UUID) -> list[DashboardTile]:
        q = (
            select(DashboardTile)
            .where(DashboardTile.dashboard_id == dashboard_id)
            .options(selectinload(DashboardTile.chart))
            .order_by(DashboardTile.group_id, DashboardTile.position)
        )
        return list((await self.s.execute(q)).scalars())

    async def _assert_group_in_dashboard(self, dashboard_id: uuid.UUID, group_id: uuid.UUID) -> DashboardGroup:
        g = await self.group(dashboard_id, group_id)
        if g is None:
            raise TileGroupMismatch(f"group {group_id} does not belong to dashboard {dashboard_id}")
        return g

    async def add_tile(
        self,
        *,
        dashboard_id: uuid.UUID,
        group_id: uuid.UUID,
        chart: SavedChart,
        title_override: str | None,
        created_by: uuid.UUID,
        w: int = 6,
        h: int = 4,
    ) -> DashboardTile:
        await self._assert_group_in_dashboard(dashboard_id, group_id)
        position = (
            int(
                (
                    await self.s.execute(
                        select(func.coalesce(func.max(DashboardTile.position), -1)).where(
                            DashboardTile.group_id == group_id
                        )
                    )
                ).scalar_one()
            )
            + 1
        )
        # simple left-to-right, top-to-bottom placement on a 12-column grid
        x = (position * w) % 12
        y = ((position * w) // 12) * h
        tile = DashboardTile(
            dashboard_id=dashboard_id,
            group_id=group_id,
            saved_chart_id=chart.id,
            title_override=title_override,
            position=position,
            x=x,
            y=y,
            w=w,
            h=h,
            created_by=created_by,
        )
        self.s.add(tile)
        await self.s.flush()
        await self.s.refresh(tile, attribute_names=["chart"])
        return tile

    async def move_tile(self, tile: DashboardTile, *, group_id: uuid.UUID) -> None:
        await self._assert_group_in_dashboard(tile.dashboard_id, group_id)
        if tile.group_id != group_id:
            position = (
                int(
                    (
                        await self.s.execute(
                            select(func.coalesce(func.max(DashboardTile.position), -1)).where(
                                DashboardTile.group_id == group_id
                            )
                        )
                    ).scalar_one()
                )
                + 1
            )
            tile.group_id = group_id
            tile.position = position
            await self.s.flush()

    async def apply_layout(self, dashboard_id: uuid.UUID, items: list[dict[str, Any]]) -> int:
        """Bulk reorder/resize after a drag.  One transaction; every group id is verified against the dashboard."""
        groups = {g.id for g in await self.groups(dashboard_id)}
        tiles = {t.id: t for t in await self.tiles(dashboard_id)}
        # assign positions per group in the order given
        per_group_pos: dict[uuid.UUID, int] = {}
        touched = 0
        for item in items:
            tile = tiles.get(item["tile_id"])
            if tile is None:
                continue
            gid = item.get("group_id") or tile.group_id
            if gid not in groups:
                raise TileGroupMismatch(f"group {gid} does not belong to dashboard {dashboard_id}")
            pos = per_group_pos.get(gid, 0)
            per_group_pos[gid] = pos + 1
            tile.group_id = gid
            tile.position = pos
            for key in ("x", "y", "w", "h"):
                if item.get(key) is not None:
                    setattr(tile, key, int(item[key]))
            touched += 1
        await self.s.flush()
        return touched

    async def delete_tile(self, tile: DashboardTile) -> None:
        await self.s.execute(delete(DashboardTile).where(DashboardTile.id == tile.id))
        await self.s.flush()

    # ── refresh log ──────────────────────────────────────────────────────────
    async def record_refresh(self, **fields: Any) -> TileRefresh:
        row = TileRefresh(**fields)
        self.s.add(row)
        await self.s.flush()
        return row
