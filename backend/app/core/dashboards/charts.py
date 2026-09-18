"""Chart library operations: pin, read, update, delete-with-409."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ConflictError, NotFoundError
from app.api.schemas.charts import ChartCreate, ChartUpdate
from app.core.auth.rbac import Principal
from app.db.models import DashboardGrant, DashboardTile, SavedChart, Turn, TurnChart
from app.db.repos.audit import AuditRepo
from app.db.repos.charts import ChartRepo


async def pin_turn_chart(session: AsyncSession, principal: Principal, body: ChartCreate, *, meta: dict) -> SavedChart:
    """Copies a ``turn_chart`` into ``saved_chart``.  Pinning is cheap and honest: no re-render, no model."""
    tc = await session.get(TurnChart, body.turn_chart_id)
    if tc is None:
        raise NotFoundError("Turn chart not found")
    turn = await session.get(Turn, tc.turn_id)
    if turn is None or turn.user_id != principal.id:
        raise NotFoundError("Turn chart not found")
    from app.db.models import ChatSession

    chat = await session.get(ChatSession, turn.session_id)
    chart = await ChartRepo(session).create(
        tenant_id=principal.tenant_id,
        owner_id=principal.id,
        data_source_id=chat.data_source_id if chat else None,
        title=body.title or tc.title,
        description=body.description,
        question=turn.question,
        sql_text=tc.sql_text,
        sql_hash=tc.sql_hash,
        chart_spec=tc.chart_spec,
        params={},
        source_turn_id=turn.id,
        render_code=tc.render_code,
        dataset_name=tc.dataset_name,
    )
    await AuditRepo(session).write(
        action="chart.create",
        tenant_id=principal.tenant_id,
        actor_user_id=principal.id,
        object_type="saved_chart",
        object_id=chart.id,
        metadata={"turn_id": str(turn.id)},
        **meta,
    )
    return chart


async def get_visible_chart(session: AsyncSession, principal: Principal, chart_id: uuid.UUID) -> SavedChart:
    """Owner, or anyone holding a grant on a dashboard where the chart is placed (needed to render tiles)."""
    chart = await ChartRepo(session).get(chart_id, tenant_id=principal.tenant_id)
    if chart is None:
        raise NotFoundError("Chart not found")
    if chart.owner_id == principal.id or principal.role.value == "admin":
        return chart
    q = (
        select(DashboardTile.id)
        .join(DashboardGrant, DashboardGrant.dashboard_id == DashboardTile.dashboard_id)
        .where(DashboardTile.saved_chart_id == chart.id, DashboardGrant.principal_id == principal.id)
        .limit(1)
    )
    if (await session.execute(q)).first() is None:
        raise NotFoundError("Chart not found")
    return chart


async def _owned_chart(session: AsyncSession, principal: Principal, chart_id: uuid.UUID) -> SavedChart:
    chart = await ChartRepo(session).get(chart_id, tenant_id=principal.tenant_id)
    if chart is None or chart.owner_id != principal.id:
        raise NotFoundError("Chart not found")
    return chart


async def update_chart(
    session: AsyncSession, principal: Principal, chart_id: uuid.UUID, body: ChartUpdate, *, meta: dict
) -> SavedChart:
    chart = await _owned_chart(session, principal, chart_id)
    changed = False
    if body.title is not None and body.title != chart.title:
        chart.title, changed = body.title, True
    if body.description is not None and body.description != chart.description:
        chart.description, changed = body.description, True
    if body.params is not None and body.params != chart.params:
        chart.params, changed = body.params, True
    if body.is_archived is not None and body.is_archived != chart.is_archived:
        chart.is_archived = body.is_archived
    if changed:
        chart.version += 1  # every tile showing this chart picks the new version up on its next load
    await session.flush()
    await AuditRepo(session).write(
        action="chart.update",
        tenant_id=principal.tenant_id,
        actor_user_id=principal.id,
        object_type="saved_chart",
        object_id=chart.id,
        metadata={"version": chart.version},
        **meta,
    )
    return chart


async def delete_chart(session: AsyncSession, principal: Principal, chart_id: uuid.UUID, *, meta: dict) -> None:
    chart = await _owned_chart(session, principal, chart_id)
    repo = ChartRepo(session)
    placements = await repo.usage(chart.id)
    if placements:
        dashboards = sorted({(str(p["dashboard_id"]), p["dashboard_name"]) for p in placements})
        raise ConflictError(
            "This chart is placed on one or more dashboards; remove those tiles or archive the chart instead.",
            code="chart_in_use",
            extra={"dashboards": [{"id": d, "name": n} for d, n in dashboards], "tile_count": len(placements)},
        )
    await repo.delete(chart)
    await AuditRepo(session).write(
        action="chart.delete",
        tenant_id=principal.tenant_id,
        actor_user_id=principal.id,
        object_type="saved_chart",
        object_id=chart.id,
        **meta,
    )
