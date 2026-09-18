"""Phases 8–9: pin → dashboard → refresh with zero LLM calls; scope-aware refresh across two users."""

from __future__ import annotations

import uuid

import openai
import pytest

from app.core.llm.client import get_llm, install_transport
from app.core.sql.normalise import sql_hash
from app.db.models import SavedChart, UserRole
from tests.fakes import ScriptedTransport, happy_path_script

CLIENT_ROWS_SQL = "SELECT client_id, client_name, project_name, budget FROM v_project_overview WHERE client_id IN (:lens_scope_client_id) ORDER BY client_id"
RENDER = "fig = px.bar(df, x='project_name', y='budget', color='client_name', title='Budget by project')\nfig.show()"


@pytest.fixture
def scripted(app):
    llm = get_llm()
    original = llm.transport

    def _install(**kw):
        t = ScriptedTransport(**kw)
        install_transport(t)
        return t

    yield _install
    llm.transport = original


@pytest.fixture
async def make_chart(app, seeded):
    from app.db.session import session_factory

    async def _make(owner_id, sql=CLIENT_ROWS_SQL, render=RENDER, title="Budget by project") -> uuid.UUID:
        async with session_factory()() as s:
            chart = SavedChart(
                tenant_id=seeded["tenant_id"],
                owner_id=owner_id,
                data_source_id=seeded["source_id"],
                title=title,
                question="budget by project",
                sql_text=sql,
                sql_hash=sql_hash(sql),
                chart_spec={"data": [{"type": "bar"}], "layout": {}},
                params={},
                render_code=render,
                dataset_name="df",
            )
            s.add(chart)
            await s.commit()
            return chart.id

    return _make


async def _dashboard_with_tile(api, chart_id) -> tuple[dict, dict]:
    d = (await api.post("/dashboards", json={"name": f"board-{uuid.uuid4().hex[:6]}"})).json()
    t = (await api.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": str(chart_id)})).json()
    return d, t


async def test_pin_then_place_then_refresh_with_provider_dead(login, scripted):
    """Phase 8 gate: the LLM client is patched to raise; the tile still renders."""
    scripted(steps=happy_path_script())
    a = await login("analyst@lens.demo")
    sid = (await a.post("/sessions", json={})).json()["id"]
    turn = (await a.post("/chat", json={"session_id": sid, "message": "burn vs milestones"})).json()
    assert turn["status"] == "ok"
    tc = turn["charts"][0]

    r = await a.post(
        "/charts", json={"turn_chart_id": tc["id"], "title": "Burn vs milestones"}, headers={"Idempotency-Key": "pin-1"}
    )
    assert r.status_code == 201, r.text
    chart = r.json()
    assert chart["sql_hash"] == tc["sql_hash"] and chart["source_turn_id"] == turn["id"]
    # the same idempotency key replays the same chart instead of creating a second one
    r2 = await a.post(
        "/charts", json={"turn_chart_id": tc["id"], "title": "Burn vs milestones"}, headers={"Idempotency-Key": "pin-1"}
    )
    assert r2.status_code == 201 and r2.json()["id"] == chart["id"] and r2.headers.get("Idempotent-Replayed") == "true"

    d, tile = await _dashboard_with_tile(a, chart["id"])

    # now the provider is dead
    scripted(steps=[], raise_exc=openai.APIConnectionError(request=None))  # type: ignore[arg-type]
    llm = get_llm()
    calls_before = llm.total_calls
    r = await a.post(f"/dashboards/{d['id']}/tiles/{tile['id']}/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["llm_calls"] == 0 and llm.total_calls == calls_before
    assert body["row_count"] and body["row_count"] > 0
    assert body["chart_spec"]["data"][0]["type"] == "bar"  # re-rendered from the stored render code
    assert body["sql_hash"] == chart["sql_hash"] and body["drifted"] is False
    assert body["trace_id"]
    # a chat turn during the outage degrades honestly, dashboards keep working
    t2 = (await a.post("/chat", json={"session_id": sid, "message": "anything"})).json()
    assert t2["status"] == "error" and t2["error_code"] == "provider_unavailable"
    r = await a.post(f"/dashboards/{d['id']}/refresh")
    assert r.status_code == 200 and r.json()["llm_calls"] == 0 and r.json()["tiles"][0]["status"] == "ok"


async def test_refresh_uses_viewers_scope_not_pinners(login, make_user, api_factory, make_chart, seeded):
    """Phase 9 gate: the same tile returns fewer rows for the narrower-scoped viewer."""
    owner = await login("analyst@lens.demo")  # unrestricted
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"])
    d, tile = await _dashboard_with_tile(owner, chart_id)

    v_id, v_email, v_pw = await make_user(UserRole.viewer, scope=[1, 2])
    await owner.put(
        f"/dashboards/{d['id']}/grants",
        json={
            "grants": [
                {"principal_id": str(seeded["users"]["analyst@lens.demo"]), "role": "owner"},
                {"principal_id": str(v_id), "role": "viewer"},
            ]
        },
    )
    viewer = api_factory()
    await viewer.login(v_email, v_pw)

    wide = (await owner.post(f"/dashboards/{d['id']}/tiles/{tile['id']}/refresh")).json()
    narrow = (await viewer.post(f"/dashboards/{d['id']}/tiles/{tile['id']}/refresh")).json()
    assert wide["status"] == narrow["status"] == "ok"
    assert narrow["row_count"] < wide["row_count"]
    client_col = narrow["columns"].index("client_id")
    assert {r[client_col] for r in narrow["rows"]} <= {1, 2}
    assert {r[client_col] for r in wide["rows"]} > {1, 2}
    assert wide["sql_hash"] == narrow["sql_hash"]  # same artefact, different binding
    assert narrow["llm_calls"] == wide["llm_calls"] == 0
    # the re-rendered chart carries only the viewer's data
    assert len(narrow["chart_spec"]["data"]) <= len(wide["chart_spec"]["data"])
    # tile_refresh rows record WHO refreshed
    from sqlalchemy import select

    from app.db.models import TileRefresh
    from app.db.session import session_factory

    async with session_factory()() as s:
        rows = list(
            (await s.execute(select(TileRefresh).where(TileRefresh.tile_id == uuid.UUID(tile["id"])))).scalars()
        )
    assert {r.viewer_id for r in rows} == {seeded["users"]["analyst@lens.demo"], v_id}
    assert {r.row_count for r in rows} == {wide["row_count"], narrow["row_count"]}


async def test_refresh_without_scope_row_renders_error_not_unscoped_data(
    login, make_user, api_factory, make_chart, seeded
):
    owner = await login("analyst@lens.demo")
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"])
    d, tile = await _dashboard_with_tile(owner, chart_id)
    n_id, n_email, n_pw = await make_user(UserRole.viewer, scope=None)  # no scope row at all
    await owner.put(
        f"/dashboards/{d['id']}/grants",
        json={
            "grants": [
                {"principal_id": str(seeded["users"]["analyst@lens.demo"]), "role": "owner"},
                {"principal_id": str(n_id), "role": "viewer"},
            ]
        },
    )
    noscope = api_factory()
    await noscope.login(n_email, n_pw)
    r = await noscope.post(f"/dashboards/{d['id']}/tiles/{tile['id']}/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "blocked" and body["rows"] == [] and body["chart_spec"] is None


async def test_refresh_when_scope_source_fails_is_an_error_state(login, make_chart, seeded, monkeypatch):
    from app.api.errors import ScopeUnavailable
    from app.core.security import access_scope

    owner = await login("analyst@lens.demo")
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"])
    d, tile = await _dashboard_with_tile(owner, chart_id)

    async def broken(self, *a, **k):
        raise ScopeUnavailable()

    monkeypatch.setattr(access_scope.DbScopeSource, "fetch", broken)
    body = (await owner.post(f"/dashboards/{d['id']}/tiles/{tile['id']}/refresh")).json()
    assert body["status"] == "error" and body["error_code"] == "scope_unavailable"
    assert body["rows"] == [] and body["chart_spec"] is None
    assert "permissions" in body["message"].lower()


async def test_refresh_of_invalid_query_says_so(login, make_chart, seeded):
    owner = await login("analyst@lens.demo")
    bad_sql = (
        "SELECT client_id, column_that_was_dropped FROM v_project_overview WHERE client_id IN (:lens_scope_client_id)"
    )
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"], sql=bad_sql)
    d, tile = await _dashboard_with_tile(owner, chart_id)
    body = (await owner.post(f"/dashboards/{d['id']}/tiles/{tile['id']}/refresh")).json()
    assert body["status"] == "invalid_query"
    assert "no longer valid" in body["message"].lower()
    assert body["rows"] == []


async def test_refresh_of_tampered_sql_is_blocked(login, make_chart, seeded):
    owner = await login("analyst@lens.demo")
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"], sql="SELECT name FROM project")
    d, tile = await _dashboard_with_tile(owner, chart_id)
    body = (await owner.post(f"/dashboards/{d['id']}/tiles/{tile['id']}/refresh")).json()
    assert body["status"] == "blocked" and body["error_code"] == "guard_blocked"


async def test_chart_run_preview_under_callers_scope(login, make_user, api_factory, make_chart, seeded):
    owner = await login("analyst@lens.demo")
    chart_id = await make_chart(seeded["users"]["analyst@lens.demo"])
    r = await owner.post(f"/charts/{chart_id}/run")
    assert r.status_code == 200 and r.json()["row_count"] > 0 and r.json()["llm_calls"] == 0
    # analyst2 (clients 1-3) cannot even see the chart: 404, not 403
    a2 = await login("analyst2@lens.demo")
    assert (await a2.post(f"/charts/{chart_id}/run")).status_code == 404
