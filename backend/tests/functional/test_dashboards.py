"""Phase 5 gate: the whole dashboard product works with manually inserted saved_chart rows.

* two users, two dashboards each; one chart placed on three dashboards;
* deleting a dashboard leaves the chart intact; deleting the chart returns 409 listing dashboards;
* a non-granted user gets 404 on EVERY dashboard route;
* a tile cannot be moved into another dashboard's group;
* exactly one owner; global viewer can never edit; group deletion never orphans tiles.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.sql.normalise import sql_hash
from app.db.models import SavedChart, UserRole

SIMPLE_SQL = "SELECT region_name, COUNT(*) AS orders FROM v_orders WHERE region_id IN (:lens_scope_region_id) GROUP BY region_name"
SIMPLE_SPEC = {
    "data": [{"type": "bar", "x": ["a", "b"], "y": [1, 2]}],
    "layout": {"title": {"text": "Projects by client"}},
}


@pytest.fixture
async def make_chart(app, seeded):
    from app.db.session import session_factory

    async def _make(owner_id: uuid.UUID, title: str = "Projects by client", sql: str = SIMPLE_SQL) -> uuid.UUID:
        async with session_factory()() as s:
            chart = SavedChart(
                tenant_id=seeded["tenant_id"],
                owner_id=owner_id,
                data_source_id=seeded["source_id"],
                title=title,
                question="how many projects per client?",
                sql_text=sql,
                sql_hash=sql_hash(sql),
                chart_spec=SIMPLE_SPEC,
                params={},
            )
            s.add(chart)
            await s.commit()
            return chart.id

    return _make


async def _create_dashboard(api, name: str) -> dict:
    r = await api.post("/dashboards", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


async def test_chart_dashboard_separation(login, make_user, api_factory, make_chart, seeded):
    a = await login("analyst@lens.demo")
    uid_b, email_b, pw_b = await make_user(UserRole.analyst, scope=["*"])
    b = api_factory()
    assert (await b.login(email_b, pw_b)).status_code == 200

    # each user creates two dashboards, born with an "Overview" group
    a1, a2 = await _create_dashboard(a, "A one"), await _create_dashboard(a, "A two")
    await _create_dashboard(b, "B one")
    await _create_dashboard(b, "B two")
    assert [g["title"] for g in a1["groups"]] == ["Overview"]
    assert (await a.post("/dashboards", json={"name": "A one"})).status_code == 409  # unique per owner
    assert (await b.post("/dashboards", json={"name": "A one"})).status_code == 201  # but not across owners

    # one chart placed on three of A's dashboards (twice on one of them) — stored ONCE
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"])
    a3 = await _create_dashboard(a, "A three")
    tiles = []
    for d in (a1, a2, a3, a3):
        r = await a.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": str(chart_id)})
        assert r.status_code == 201, r.text
        tiles.append(r.json())
    assert len({t["sql_hash"] for t in tiles}) == 1
    usage = (await a.get(f"/charts/{chart_id}/usage")).json()
    assert usage["dashboard_count"] == 3 and len(usage["placements"]) == 4
    listed = (await a.get("/charts")).json()
    assert listed["total"] == 1 and listed["items"][0]["placement_count"] == 4

    # deleting the chart while placed → 409 listing the dashboards
    r = await a.delete(f"/charts/{chart_id}")
    assert r.status_code == 409
    body = r.json()
    assert body["type"].endswith("chart_in_use")
    assert {d["name"] for d in body["dashboards"]} == {"A one", "A two", "A three"}

    # deleting a dashboard cascades tiles and groups but leaves the chart intact
    assert (await a.delete(f"/dashboards/{a1['id']}")).status_code == 204
    assert (await a.get(f"/dashboards/{a1['id']}")).status_code == 404
    assert (await a.get(f"/charts/{chart_id}")).status_code == 200
    assert (await a.get(f"/charts/{chart_id}/usage")).json()["dashboard_count"] == 2

    # remove the remaining placements, then delete succeeds
    for d in (a2, a3):
        await a.delete(f"/dashboards/{d['id']}")
    assert (await a.get(f"/charts/{chart_id}/usage")).json()["dashboard_count"] == 0
    assert (await a.delete(f"/charts/{chart_id}")).status_code == 204
    assert (await a.get(f"/charts/{chart_id}")).status_code == 404

    # B never saw any of it
    assert (await b.get(f"/charts/{chart_id}")).status_code == 404
    assert {d["name"] for d in (await b.get("/dashboards")).json()} == {"B one", "B two", "A one"}


DASHBOARD_ROUTES = [
    ("GET", "/dashboards/{d}", None),
    ("PATCH", "/dashboards/{d}", {"name": "x"}),
    ("DELETE", "/dashboards/{d}", None),
    ("POST", "/dashboards/{d}/groups", {"title": "x"}),
    ("PATCH", "/dashboards/{d}/groups/{g}", {"title": "x"}),
    ("DELETE", "/dashboards/{d}/groups/{g}", None),
    ("POST", "/dashboards/{d}/tiles", {"saved_chart_id": "{c}"}),
    ("PATCH", "/dashboards/{d}/tiles/{t}", {"w": 4}),
    ("DELETE", "/dashboards/{d}/tiles/{t}", None),
    ("PUT", "/dashboards/{d}/layout", {"items": []}),
    ("POST", "/dashboards/{d}/tiles/{t}/refresh", None),
    ("POST", "/dashboards/{d}/refresh", None),
    ("GET", "/dashboards/{d}/grants", None),
    ("PUT", "/dashboards/{d}/grants", {"grants": [{"principal_id": "{u}", "role": "owner"}]}),
    ("POST", "/dashboards/{d}/suggest-groups", None),
    ("POST", "/dashboards/{d}/apply-grouping", {"proposal_id": "abcdefgh", "groups": [{"title": "x", "tile_ids": []}]}),
]


@pytest.mark.parametrize("method,path,body", DASHBOARD_ROUTES, ids=[f"{m} {p}" for m, p, _ in DASHBOARD_ROUTES])
async def test_non_granted_user_gets_404_on_every_dashboard_route(
    login, make_user, api_factory, make_chart, seeded, method, path, body
):
    owner = await login("analyst@lens.demo")
    d = await _create_dashboard(owner, "private board")
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"])
    tile = (await owner.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": str(chart_id)})).json()
    group = d["groups"][0]

    uid, email, pw = await make_user(UserRole.admin, scope=["*"])  # even an admin without a grant: 404
    other = api_factory()
    await other.login(email, pw)
    url = path.format(d=d["id"], g=group["id"], t=tile["id"])
    if body is not None:
        body = {k: (v.format(c=chart_id, u=uid) if isinstance(v, str) else v) for k, v in body.items()}
        if "grants" in body:
            body = {"grants": [{"principal_id": str(uid), "role": "owner"}]}
    r = (
        await getattr(other, method.lower())(url, json=body)
        if body is not None
        else await getattr(other, method.lower())(url)
    )
    assert r.status_code == 404, (method, path, r.text)


async def test_tile_cannot_move_into_another_dashboards_group(login, make_chart, seeded):
    a = await login("analyst@lens.demo")
    d1, d2 = await _create_dashboard(a, "d1"), await _create_dashboard(a, "d2")
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"])
    tile = (await a.post(f"/dashboards/{d1['id']}/tiles", json={"saved_chart_id": str(chart_id)})).json()
    foreign_group = d2["groups"][0]["id"]
    r = await a.patch(f"/dashboards/{d1['id']}/tiles/{tile['id']}", json={"group_id": foreign_group})
    assert r.status_code == 404
    r = await a.post(f"/dashboards/{d1['id']}/tiles", json={"saved_chart_id": str(chart_id), "group_id": foreign_group})
    assert r.status_code == 404
    r = await a.put(
        f"/dashboards/{d1['id']}/layout", json={"items": [{"tile_id": tile["id"], "group_id": foreign_group}]}
    )
    assert r.status_code == 422
    # and the repository invariant holds directly, not only through the API
    from app.db.repos.dashboards import DashboardRepo, TileGroupMismatch
    from app.db.session import session_factory

    async with session_factory()() as s:
        repo = DashboardRepo(s)
        t = await repo.tile(uuid.UUID(d1["id"]), uuid.UUID(tile["id"]))
        with pytest.raises(TileGroupMismatch):
            await repo.move_tile(t, group_id=uuid.UUID(foreign_group))


async def test_delete_tile_removes_placement_but_keeps_chart(login, make_user, api_factory, make_chart, seeded):
    owner = await login("analyst@lens.demo")
    d = await _create_dashboard(owner, "removable tiles")
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"])
    tile = (await owner.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": str(chart_id)})).json()

    # a viewer grant cannot delete the tile — should fail loudly (403), never silently no-op
    viewer_id, viewer_email, viewer_pw = await make_user(UserRole.analyst, scope=["*"])
    viewer = api_factory()
    assert (await viewer.login(viewer_email, viewer_pw)).status_code == 200
    await owner.put(f"/dashboards/{d['id']}/grants", json={"grants": [
        {"principal_id": str(seeded["users"]["analyst@lens.demo"]), "role": "owner"},
        {"principal_id": str(viewer_id), "role": "viewer"},
    ]})
    r = await viewer.delete(f"/dashboards/{d['id']}/tiles/{tile['id']}")
    assert r.status_code == 403
    full = (await owner.get(f"/dashboards/{d['id']}")).json()
    assert len(full["groups"][0]["tiles"]) == 1  # still there

    # the owner can delete it: placement gone, chart untouched
    r = await owner.delete(f"/dashboards/{d['id']}/tiles/{tile['id']}")
    assert r.status_code == 204
    full = (await owner.get(f"/dashboards/{d['id']}")).json()
    assert full["groups"][0]["tiles"] == []
    assert (await owner.get(f"/charts/{chart_id}")).status_code == 200

    # deleting an already-deleted tile is a clean 404, not a silent success
    assert (await owner.delete(f"/dashboards/{d['id']}/tiles/{tile['id']}")).status_code == 404


async def test_group_delete_moves_tiles_to_default_group(login, make_chart, seeded):
    a = await login("analyst@lens.demo")
    d = await _create_dashboard(a, "grouped")
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"])
    g = (await a.post(f"/dashboards/{d['id']}/groups", json={"title": "Finance"})).json()
    t1 = (
        await a.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": str(chart_id), "group_id": g["id"]})
    ).json()
    t2 = (
        await a.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": str(chart_id), "group_id": g["id"]})
    ).json()
    assert (await a.post(f"/dashboards/{d['id']}/groups", json={"title": "x" * 61})).status_code == 422
    r = await a.delete(f"/dashboards/{d['id']}/groups/{g['id']}")
    assert r.status_code == 200 and "2 tile(s)" in r.json()["message"]
    full = (await a.get(f"/dashboards/{d['id']}")).json()
    assert len(full["groups"]) == 1
    assert {t["id"] for t in full["groups"][0]["tiles"]} == {t1["id"], t2["id"]}
    # the default group cannot be deleted
    assert (await a.delete(f"/dashboards/{d['id']}/groups/{full['groups'][0]['id']}")).status_code == 409


async def test_grants_effective_role_and_single_owner(login, make_user, api_factory, make_chart, seeded):
    owner = await login("analyst@lens.demo")
    d = await _create_dashboard(owner, "shared board")
    owner_id = seeded["users"]["analyst@lens.demo"]

    # a global VIEWER with an editor grant is still just a viewer (ceiling)
    v_id, v_email, v_pw = await make_user(UserRole.viewer, scope=[1, 2])
    e_id, e_email, e_pw = await make_user(UserRole.analyst, scope=["*"])
    r = await owner.put(
        f"/dashboards/{d['id']}/grants",
        json={
            "grants": [
                {"principal_id": str(owner_id), "role": "owner"},
                {"principal_id": str(v_id), "role": "editor"},
                {"principal_id": str(e_id), "role": "editor"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    viewer, editor = api_factory(), api_factory()
    await viewer.login(v_email, v_pw)
    await editor.login(e_email, e_pw)
    listed = (await viewer.get("/dashboards")).json()
    assert listed[0]["effective_role"] == "viewer"
    assert (await viewer.get(f"/dashboards/{d['id']}")).status_code == 200
    assert (await viewer.post(f"/dashboards/{d['id']}/groups", json={"title": "nope"})).status_code == 403
    assert (await editor.post(f"/dashboards/{d['id']}/groups", json={"title": "ok"})).status_code == 201
    # an editor may place a chart they can see (via this dashboard), but not delete the dashboard or re-share it
    assert (await editor.delete(f"/dashboards/{d['id']}")).status_code == 403
    assert (
        await editor.put(
            f"/dashboards/{d['id']}/grants", json={"grants": [{"principal_id": str(e_id), "role": "owner"}]}
        )
    ).status_code == 403

    # exactly one owner: zero or two owners are refused
    r = await owner.put(
        f"/dashboards/{d['id']}/grants", json={"grants": [{"principal_id": str(e_id), "role": "editor"}]}
    )
    assert r.status_code == 409
    r = await owner.put(
        f"/dashboards/{d['id']}/grants",
        json={
            "grants": [{"principal_id": str(owner_id), "role": "owner"}, {"principal_id": str(e_id), "role": "owner"}]
        },
    )
    assert r.status_code == 409
    # ownership transfer: the new owner can delete, the old owner (now editor) cannot
    r = await owner.put(
        f"/dashboards/{d['id']}/grants",
        json={
            "grants": [{"principal_id": str(owner_id), "role": "editor"}, {"principal_id": str(e_id), "role": "owner"}]
        },
    )
    assert r.status_code == 200
    assert (await owner.delete(f"/dashboards/{d['id']}")).status_code == 403
    assert (await editor.delete(f"/dashboards/{d['id']}")).status_code == 204
    assert (await owner.get(f"/dashboards/{d['id']}")).status_code == 404


async def test_tenant_visibility_gives_implicit_viewer_grant(login, make_user, api_factory):
    owner = await login("analyst@lens.demo")
    d = await _create_dashboard(owner, "tenant-wide")
    _, email, pw = await make_user(UserRole.viewer, scope=[1])
    other = api_factory()
    await other.login(email, pw)
    assert (await other.get(f"/dashboards/{d['id']}")).status_code == 404
    assert (await owner.patch(f"/dashboards/{d['id']}", json={"visibility": "tenant"})).status_code == 200
    r = await other.get(f"/dashboards/{d['id']}")
    assert r.status_code == 200 and r.json()["effective_role"] == "viewer"
    assert (await other.patch(f"/dashboards/{d['id']}", json={"name": "hijack"})).status_code == 403


async def test_layout_bulk_update_and_tile_overrides_reject_sql(login, make_chart, seeded):
    a = await login("analyst@lens.demo")
    d = await _create_dashboard(a, "layout")
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"])
    t1 = (await a.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": str(chart_id)})).json()
    t2 = (await a.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": str(chart_id)})).json()
    g2 = (await a.post(f"/dashboards/{d['id']}/groups", json={"title": "Second"})).json()
    r = await a.put(
        f"/dashboards/{d['id']}/layout",
        json={
            "items": [
                {"tile_id": t2["id"], "group_id": g2["id"], "x": 0, "y": 0, "w": 12, "h": 6},
                {"tile_id": t1["id"], "x": 6, "y": 0, "w": 6, "h": 4},
            ]
        },
    )
    assert r.status_code == 200, r.text
    groups = {g["title"]: g for g in r.json()["groups"]}
    assert groups["Second"]["tiles"][0]["id"] == t2["id"] and groups["Second"]["tiles"][0]["w"] == 12
    assert groups["Overview"]["tiles"][0]["id"] == t1["id"] and groups["Overview"]["tiles"][0]["x"] == 6
    # overrides are presentation only
    r = await a.patch(f"/dashboards/{d['id']}/tiles/{t1['id']}", json={"overrides": {"sql": "DROP TABLE x"}})
    assert r.status_code == 422
    r = await a.patch(
        f"/dashboards/{d['id']}/tiles/{t1['id']}",
        json={"overrides": {"colorway": ["#123"]}, "title_override": "Renamed"},
    )
    assert r.status_code == 200 and r.json()["title"] == "Renamed" and r.json()["sql_hash"] == t1["sql_hash"]


async def test_chart_update_bumps_version_seen_by_every_tile(login, make_chart, seeded):
    a = await login("analyst@lens.demo")
    d1, d2 = await _create_dashboard(a, "v1"), await _create_dashboard(a, "v2")
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"])
    for d in (d1, d2):
        await a.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": str(chart_id)})
    r = await a.patch(f"/charts/{chart_id}", json={"title": "Renamed chart"})
    assert r.status_code == 200 and r.json()["version"] == 2
    for d in (d1, d2):
        tile = (await a.get(f"/dashboards/{d['id']}")).json()["groups"][0]["tiles"][0]
        assert tile["title"] == "Renamed chart" and tile["chart_version"] == 2


async def test_viewer_cannot_create_dashboards_or_charts(login):
    partner = await login("partner@lens.demo")
    assert (await partner.post("/dashboards", json={"name": "nope"})).status_code == 403
    assert (await partner.post("/charts", json={"turn_chart_id": str(uuid.uuid4())})).status_code == 403
    assert (await partner.get("/dashboards")).status_code == 200
