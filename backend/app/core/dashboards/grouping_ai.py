"""LLM-assisted grouping: **model proposes, schema validates, code applies, user owns.**

``suggest_grouping`` sends tile titles and questions only — never data, never
specs — and returns a strict :class:`GroupingProposal`.  Deterministic code then
checks completeness (every tile exactly once, every id real, nothing invented)
and renders a diff.  Nothing moves until the user posts the confirmed proposal
back to ``apply_grouping``, which is plain code in a single transaction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DashboardContext
from app.api.errors import ProviderUnavailable, ValidationFailed
from app.api.schemas.dashboards import ApplyGroupingOut, ApplyGroupingRequest, GroupingProposalOut, GroupSuggestionOut
from app.core.cache import get_redis
from app.core.llm.client import get_llm
from app.core.llm.model_selector import Stage
from app.core.security.prompt_injection import wrap_data
from app.db.models import DashboardGroup, DashboardTile
from app.db.repos.dashboards import DEFAULT_GROUP_TITLE, DashboardRepo
from app.db.seed.analytics_catalog import DATASET
from app.prompts import get_prompt


class GroupSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=60)
    tile_ids: list[str]


class GroupingProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groups: list[GroupSuggestion]
    rationale: str = Field(max_length=400)


class CompletenessError(ValueError):
    pass


def check_completeness(groups: list[tuple[str, list[uuid.UUID]]], tile_ids: set[uuid.UUID]) -> None:
    """Every tile assigned exactly once, every id real, no ids invented, titles non-empty and ≤ 60 chars."""
    seen: list[uuid.UUID] = []
    for title, ids in groups:
        if not title.strip() or len(title) > 60:
            raise CompletenessError(f"invalid group title {title!r}")
        for tid in ids:
            if tid not in tile_ids:
                raise CompletenessError(f"proposal references an unknown tile {tid}")
            seen.append(tid)
    if len(seen) != len(set(seen)):
        raise CompletenessError("a tile is assigned to more than one group")
    missing = tile_ids - set(seen)
    if missing:
        raise CompletenessError(f"{len(missing)} tile(s) are not assigned to any group")
    if not groups:
        raise CompletenessError("no groups proposed")


def render_diff(
    groups: list[tuple[str, list[uuid.UUID]]], tiles: list[DashboardTile], current_groups: list[DashboardGroup]
) -> list[dict[str, Any]]:
    group_title = {g.id: g.title for g in current_groups}
    tile_by_id = {t.id: t for t in tiles}
    diff = []
    for title, ids in groups:
        for tid in ids:
            t = tile_by_id[tid]
            before = group_title.get(t.group_id, "?")
            diff.append(
                {
                    "tile_id": str(tid),
                    "title": t.title_override or t.chart.title,
                    "from": before,
                    "to": title,
                    "changed": before != title,
                }
            )
    return diff


def _proposal_id(dashboard_id: uuid.UUID, groups: list[tuple[str, list[uuid.UUID]]]) -> str:
    canonical = json.dumps([[t, [str(i) for i in ids]] for t, ids in groups], sort_keys=True)
    return hashlib.sha256(f"{dashboard_id}:{canonical}".encode()).hexdigest()[:32]


async def suggest_grouping(session: AsyncSession, ctx: DashboardContext) -> GroupingProposalOut:
    repo = DashboardRepo(session)
    tiles = await repo.tiles(ctx.dashboard_id)
    if len(tiles) < 2:
        raise ValidationFailed("At least two tiles are needed to suggest groups")
    current = await repo.groups(ctx.dashboard_id)
    prompt = get_prompt("grouping")
    listing = "\n".join(
        f"- id: {t.id} | title: {(t.title_override or t.chart.title)[:120]} | question: {t.chart.question[:200]}"
        for t in tiles
    )
    resp = await get_llm().complete(
        Stage.grouping,
        [
            {"role": "system", "content": prompt.render(group_examples=DATASET.group_examples)},
            {"role": "user", "content": wrap_data("tiles", listing)},
        ],
        response_model=GroupingProposal,
        max_tokens=2500,
        prompt_version_id=prompt.version_id,
    )
    proposal = resp.parsed
    if not isinstance(proposal, GroupingProposal):
        raise ProviderUnavailable("No grouping proposal was produced")
    groups: list[tuple[str, list[uuid.UUID]]] = []
    for g in proposal.groups:
        ids = []
        for raw in g.tile_ids:
            try:
                ids.append(uuid.UUID(str(raw)))
            except ValueError:
                raise ValidationFailed(
                    f"The model proposed an invalid tile id ({raw[:40]}); nothing was changed"
                ) from None
        groups.append((g.title.strip(), ids))
    try:
        check_completeness(groups, {t.id for t in tiles})
    except CompletenessError as exc:
        raise ValidationFailed(
            f"The proposal was rejected by the completeness check: {exc}. Nothing was changed."
        ) from None
    pid = _proposal_id(ctx.dashboard_id, groups)
    redis = get_redis()
    if redis is not None:
        try:
            await redis.set(f"grouping:{ctx.dashboard_id}:{pid}", "1", ex=3600)
        except Exception:
            pass
    return GroupingProposalOut(
        proposal_id=pid,
        groups=[GroupSuggestionOut(title=t, tile_ids=ids) for t, ids in groups],
        rationale=proposal.rationale,
        diff=render_diff(groups, tiles, current),
    )


async def apply_grouping(session: AsyncSession, ctx: DashboardContext, body: ApplyGroupingRequest) -> ApplyGroupingOut:
    """Deterministic application of a confirmed proposal.  Re-validated; single transaction (the request's session)."""
    repo = DashboardRepo(session)
    tiles = await repo.tiles(ctx.dashboard_id)
    groups = [(g.title.strip(), list(g.tile_ids)) for g in body.groups]
    try:
        check_completeness(groups, {t.id for t in tiles})
    except CompletenessError as exc:
        raise ValidationFailed(str(exc)) from None
    if _proposal_id(ctx.dashboard_id, groups) != body.proposal_id:
        raise ValidationFailed("proposal_id does not match the submitted groups; request a fresh suggestion")
    existing = {g.title: g for g in await repo.groups(ctx.dashboard_id)}
    created = moved = 0
    tile_by_id = {t.id: t for t in tiles}
    target_ids: list[uuid.UUID] = []
    for title, ids in groups:
        group = existing.get(title)
        if group is None:
            group = await repo.add_group(ctx.dashboard_id, title)
            created += 1
        target_ids.append(group.id)
        for pos, tid in enumerate(ids):
            tile = tile_by_id[tid]
            if tile.group_id != group.id:
                moved += 1
            tile.group_id = group.id
            tile.position = pos
    await session.flush()
    # groups left empty are removed, except the default one; then re-number positions in proposal order
    removed = 0
    remaining = await repo.groups(ctx.dashboard_id)
    occupied = {t.group_id for t in await repo.tiles(ctx.dashboard_id)}
    for g in remaining:
        if g.id not in occupied and g.id not in target_ids and g.title != DEFAULT_GROUP_TITLE:
            await session.delete(g)
            removed += 1
    await session.flush()
    remaining = await repo.groups(ctx.dashboard_id)
    order = [gid for gid in target_ids] + [g.id for g in remaining if g.id not in target_ids]
    await repo.reorder_groups(ctx.dashboard_id, order)
    return ApplyGroupingOut(
        dashboard_id=ctx.dashboard_id, groups_created=created, tiles_moved=moved, groups_removed=removed
    )
