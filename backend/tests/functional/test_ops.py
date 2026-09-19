"""Phase 10 gate: one trace_id from a chat response retrieves a complete span tree; usage; metrics; allow-list admin."""

from __future__ import annotations

import pytest

from app.core.llm.client import get_llm, install_transport
from app.db.seed.analytics_catalog import DATASET
from tests.fakes import ScriptedTransport, happy_path_script


@pytest.fixture
def scripted(app):
    llm = get_llm()
    original = llm.transport
    install_transport(ScriptedTransport(steps=happy_path_script()))
    yield
    llm.transport = original


def _names(nodes, acc=None):
    acc = acc if acc is not None else []
    for n in nodes:
        acc.append(n["name"])
        _names(n["children"], acc)
    return acc


async def test_trace_tree_for_a_turn(login, scripted):
    a = await login("analyst@lens.demo")
    sid = (await a.post("/sessions", json={})).json()["id"]
    turn = (await a.post("/chat", json={"session_id": sid, "message": "burn vs milestones"})).json()
    assert turn["status"] == "ok"
    admin = await login("admin@lens.demo")
    r = await admin.get(f"/traces/{turn['trace_id']}")
    assert r.status_code == 200, r.text
    tr = r.json()
    names = _names(tr["roots"])
    assert len(tr["roots"]) == 1 and tr["roots"][0]["name"].startswith("http POST")
    for expected in (
        "screen",
        "context",
        "agent",
        "llm.agent",
        "sql.guard",
        "sql.execute",
        "python.execute",
        "narrative",
        "llm.screen",
        "llm.narrative",
    ):
        assert expected in names, (expected, names)
    assert tr["llm_calls"] == turn["llm_calls"]
    assert tr["turn"]["id"] == turn["id"] and tr["turn"]["prompt_version_id"].startswith("agent_system@")
    llm_span = next(n for n in _walk(tr["roots"]) if n["name"] == "llm.agent")
    assert (
        llm_span["attributes"]["prompt_version_id"].startswith("agent_system@")
        and "input_tokens" in llm_span["attributes"]
    )
    # the owner of the turn may read their own trace; another analyst may not
    assert (await a.get(f"/traces/{turn['trace_id']}")).status_code == 200
    other = await login("analyst2@lens.demo")
    assert (await other.get(f"/traces/{turn['trace_id']}")).status_code == 404


def _walk(nodes):
    for n in nodes:
        yield n
        yield from _walk(n["children"])


async def test_refresh_is_its_own_trace_with_zero_llm_spans(login, scripted):
    a = await login("analyst@lens.demo")
    sid = (await a.post("/sessions", json={})).json()["id"]
    turn = (await a.post("/chat", json={"session_id": sid, "message": "burn"})).json()
    chart = (await a.post("/charts", json={"turn_chart_id": turn["charts"][0]["id"]})).json()
    d = (await a.post("/dashboards", json={"name": "traced"})).json()
    tile = (await a.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": chart["id"]})).json()
    ref = (await a.post(f"/dashboards/{d['id']}/tiles/{tile['id']}/refresh")).json()
    assert ref["trace_id"] != turn["trace_id"]
    admin = await login("admin@lens.demo")
    tr = (await admin.get(f"/traces/{ref['trace_id']}")).json()
    names = _names(tr["roots"])
    assert tr["roots"][0]["name"] == "tile.refresh"
    assert "sql.guard" in names and "sql.execute" in names and "chart.render" in names
    assert tr["llm_calls"] == 0 and not any(n.startswith("llm.") for n in names)


async def test_usage_accounting_and_visibility(login, scripted):
    a = await login("analyst@lens.demo")
    sid = (await a.post("/sessions", json={})).json()["id"]
    turn = (await a.post("/chat", json={"session_id": sid, "message": "burn"})).json()
    mine = (await a.get("/usage")).json()
    assert len(mine) == 1 and mine[0]["turn_count"] >= 1 and mine[0]["input_tokens"] >= turn["input_tokens"]
    other = await login("analyst2@lens.demo")
    assert (await other.get(f"/usage?user_id={mine[0]['user_id']}")).status_code == 403
    admin = await login("admin@lens.demo")
    assert any(row["email"] == "analyst@lens.demo" for row in (await admin.get("/usage")).json())


async def test_metrics_endpoint(api):
    r = await api.get("/metrics")
    assert r.status_code == 200 and "lens_http_requests_total" in r.text and "lens_guard_blocks_total" in r.text


async def test_allow_list_admin_and_sensitive_columns_hidden(login, seeded):
    analyst = await login("analyst@lens.demo")
    views = (await analyst.get(f"/sources/{seeded['source_id']}/views")).json()
    overview = next(v for v in views if v["view_name"] == "v_customers")
    assert not any(c["name"] == "phone" for c in overview["column_metadata"])
    assert (
        await analyst.patch(f"/sources/{seeded['source_id']}/views/{overview['id']}", json={"is_enabled": False})
    ).status_code == 403
    admin = await login("admin@lens.demo")
    admin_views = (await admin.get(f"/sources/{seeded['source_id']}/views")).json()
    assert any(
        c["name"] == "phone" and c["sensitive"]
        for v in admin_views
        if v["view_name"] == "v_customers"
        for c in v["column_metadata"]
    )
    original_rules = overview["business_rules"]
    r = await admin.patch(f"/sources/{seeded['source_id']}/views/{overview['id']}", json={"business_rules": "edited"})
    assert r.status_code == 200 and r.json()["business_rules"] == "edited"
    await admin.patch(f"/sources/{seeded['source_id']}/views/{overview['id']}", json={"business_rules": original_rules})
    r = await admin.post(
        "/sources", json={"name": "x", "dsn_secret_ref": "postgresql://u:p@h/db", "read_only_role": "r"}
    )
    assert r.status_code == 422  # a DSN is not a secret ref
    partner_id = str(seeded["users"]["partner@lens.demo"])
    r = await admin.put(f"/sources/{seeded['source_id']}/scopes", json={"user_id": partner_id, "scope_values": [1, 2]})
    assert r.status_code == 200 and r.json()["scope_values"] == [1, 2]
    # restore the seeded scope: the suite runs against the shared dev DB and the demo partner must stay single-region
    r = await admin.put(
        f"/sources/{seeded['source_id']}/scopes",
        json={"user_id": partner_id, "scope_values": list(DATASET.demo_scope_single)},
    )
    assert r.status_code == 200 and r.json()["scope_values"] == list(DATASET.demo_scope_single)


async def test_refresher_can_read_their_own_refresh_trace(login, scripted):
    a = await login("analyst@lens.demo")
    sid = (await a.post("/sessions", json={})).json()["id"]
    turn = (await a.post("/chat", json={"session_id": sid, "message": "burn"})).json()
    chart = (await a.post("/charts", json={"turn_chart_id": turn["charts"][0]["id"]})).json()
    d = (await a.post("/dashboards", json={"name": "own trace"})).json()
    tile = (await a.post(f"/dashboards/{d['id']}/tiles", json={"saved_chart_id": chart["id"]})).json()
    ref = (await a.post(f"/dashboards/{d['id']}/tiles/{tile['id']}/refresh")).json()
    assert (await a.get(f"/traces/{ref['trace_id']}")).status_code == 200
    other = await login("analyst2@lens.demo")
    assert (await other.get(f"/traces/{ref['trace_id']}")).status_code == 404
