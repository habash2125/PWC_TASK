"""Phase 11: grouping suggestion with the confirm-diff flow.  Model proposes, schema validates, code applies."""

from __future__ import annotations

import uuid

import pytest

from app.core.dashboards.grouping_ai import CompletenessError, check_completeness
from app.core.llm.client import get_llm, install_transport
from app.core.sql.normalise import sql_hash
from app.db.models import SavedChart
from tests.fakes import ScriptedTransport

SQL = "SELECT region_name, COUNT(*) AS n FROM v_orders WHERE region_id IN (:lens_scope_region_id) GROUP BY region_name"


@pytest.fixture
async def board(login, app, seeded):
    from app.db.session import session_factory

    a = await login("analyst@lens.demo")
    d = (await a.post("/dashboards", json={"name": "to group"})).json()
    tiles = []
    for title in ("Budget burn by project", "Milestone completion", "Invoice ageing", "Utilisation by practice"):
        async with session_factory()() as s:
            c = SavedChart(
                tenant_id=seeded["tenant_id"],
                owner_id=seeded["users"]["analyst@lens.demo"],
                data_source_id=seeded["source_id"],
                title=title,
                question=f"show {title.lower()}",
                sql_text=SQL,
                sql_hash=sql_hash(SQL),
                chart_spec={"data": [{"type": "bar"}], "layout": {}},
                params={},
            )
            s.add(c)
            await s.commit()
            cid = c.id
        tiles.append((await a.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": str(cid)})).json())
    return a, d, tiles


def _scripted(grouping):
    llm = get_llm()
    install_transport(ScriptedTransport(steps=[], grouping=grouping))
    return llm


async def test_suggest_then_confirm_applies_in_one_transaction(board):
    a, d, tiles = board
    ids = [t["id"] for t in tiles]
    _scripted(
        {
            "groups": [
                {"title": "Money", "tile_ids": [ids[0], ids[2]]},
                {"title": "Delivery & people", "tile_ids": [ids[1], ids[3]]},
            ],
            "rationale": "Financial tiles together; delivery and staffing together.",
        }
    )
    before = (await a.get(f"/dashboards/{d['id']}")).json()
    assert len(before["groups"]) == 1
    r = await a.post(f"/dashboards/{d['id']}/suggest-groups")
    assert r.status_code == 200, r.text
    proposal = r.json()
    assert [g["title"] for g in proposal["groups"]] == ["Money", "Delivery & people"]
    assert all(row["from"] == "Overview" and row["changed"] for row in proposal["diff"]) and len(proposal["diff"]) == 4
    # nothing moved yet
    assert len((await a.get(f"/dashboards/{d['id']}")).json()["groups"]) == 1
    # confirm
    r = await a.post(
        f"/dashboards/{d['id']}/apply-grouping",
        json={"proposal_id": proposal["proposal_id"], "groups": proposal["groups"]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"dashboard_id": d["id"], "groups_created": 2, "tiles_moved": 4, "groups_removed": 0}
    after = (await a.get(f"/dashboards/{d['id']}")).json()
    titles = [g["title"] for g in after["groups"]]
    assert titles == ["Money", "Delivery & people", "Overview"]
    assert {t["id"] for t in after["groups"][0]["tiles"]} == {ids[0], ids[2]}
    # the model's calls carried titles and questions only: no specs, no data
    llm = get_llm()
    sent = llm.transport.calls[-1]["messages"][-1]["content"]
    assert "Budget burn by project" in sent and "chart_spec" not in sent and "bar" not in sent


async def test_incomplete_or_invented_proposals_are_rejected_before_the_user_sees_them(board):
    a, d, tiles = board
    ids = [t["id"] for t in tiles]
    _scripted({"groups": [{"title": "Only some", "tile_ids": ids[:2]}], "rationale": "x"})
    r = await a.post(f"/dashboards/{d['id']}/suggest-groups")
    assert r.status_code == 422 and "not assigned" in r.json()["detail"]
    _scripted({"groups": [{"title": "Made up", "tile_ids": ids + [str(uuid.uuid4())]}], "rationale": "x"})
    r = await a.post(f"/dashboards/{d['id']}/suggest-groups")
    assert r.status_code == 422 and "unknown tile" in r.json()["detail"]
    _scripted(
        {"groups": [{"title": "Dup", "tile_ids": ids}, {"title": "Dup2", "tile_ids": [ids[0]]}], "rationale": "x"}
    )
    assert (await a.post(f"/dashboards/{d['id']}/suggest-groups")).status_code == 422
    assert len((await a.get(f"/dashboards/{d['id']}")).json()["groups"]) == 1  # nothing moved


async def test_apply_rejects_tampered_or_stale_proposals(board):
    a, d, tiles = board
    ids = [t["id"] for t in tiles]
    groups = [{"title": "A", "tile_ids": ids[:2]}, {"title": "B", "tile_ids": ids[2:]}]
    r = await a.post(
        f"/dashboards/{d['id']}/apply-grouping", json={"proposal_id": "deadbeefdeadbeef", "groups": groups}
    )
    assert r.status_code == 422 and "proposal_id" in r.json()["detail"]
    r = await a.post(
        f"/dashboards/{d['id']}/apply-grouping",
        json={"proposal_id": "deadbeefdeadbeef", "groups": [{"title": "A", "tile_ids": ids[:1]}]},
    )
    assert r.status_code == 422


def test_completeness_check_unit():
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    check_completeness([("a", [t1]), ("b", [t2])], {t1, t2})
    with pytest.raises(CompletenessError):
        check_completeness([("a", [t1])], {t1, t2})
    with pytest.raises(CompletenessError):
        check_completeness([("a", [t1, t1]), ("b", [t2])], {t1, t2})
    with pytest.raises(CompletenessError):
        check_completeness([("x" * 61, [t1, t2])], {t1, t2})
