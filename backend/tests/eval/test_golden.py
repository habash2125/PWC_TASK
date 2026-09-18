"""Golden-set evaluation.

* ``test_golden_reference`` — hermetic: reference SQL through the real guard and the read-only role.
* ``test_adversarial_prompts`` — hermetic where the pre-screen catches it; the model screen otherwise (live only).
* ``test_golden_live`` — generates SQL from the question with the configured provider (skipped without a key),
  and asserts the acceptance criteria: ≥ 90 % runnable first attempt, ≥ 85 % matching the reference.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from app.core.security.access_scope import ScopePredicate, find_scoped_references, parse_one
from app.core.security.prompt_injection import prescreen
from app.core.sql.schema_context import load_allow_list
from app.core.sql.sql_guard import SqlGuard
from app.db.analytics_pool import execute_readonly
from tests.eval.golden_set import ADVERSARIAL_PROMPTS, GOLDEN

WIDE = ScopePredicate("client_id", (), unrestricted=True)
LIVE = os.environ.get("LENS_LIVE_EVAL") == "1"


@pytest.fixture(scope="module")
async def allow(app, seeded):
    from app.db.session import session_factory

    async with session_factory()() as s:
        return await load_allow_list(s, seeded["source_id"])


@pytest.mark.parametrize("case", GOLDEN, ids=[g["id"] for g in GOLDEN])
async def test_golden_reference(case, allow, settings):
    guard = SqlGuard(settings)
    result = await guard.check(case["reference_sql"], allow=allow, scope=WIDE, mode="refresh")
    assert result.verdict == "allowed", f"{case['id']}: {result.reason}"
    check = find_scoped_references(parse_one(result.sql_canonical), allow.scope_columns)
    assert check.ok and check.references, "every reference SQL must touch a scoped view with the predicate in place"
    rows = await execute_readonly(result.sql_bound, statement_timeout_ms=8000, max_rows=5000)
    assert case["key_column"] in rows.columns, (case["id"], rows.columns)
    assert rows.row_count >= case["min_rows"], (case["id"], rows.row_count)


@pytest.mark.parametrize("case_id,prompt", ADVERSARIAL_PROMPTS, ids=[c[0] for c in ADVERSARIAL_PROMPTS])
async def test_adversarial_prompts_blocked_by_prescreen(case_id, prompt):
    """These fixtures never cost a model call: the deterministic pre-screen refuses them."""
    hit = prescreen(prompt)
    assert hit is not None, f"{case_id} should be caught by the pre-screen"


def _key_values(columns, rows, key):
    idx = columns.index(key)
    return {str(r[idx]) for r in rows}


def _figures(columns, rows) -> set[str]:
    out = set()
    for r in rows[:5]:
        for v in r:
            if isinstance(v, (int, float, Decimal)) and v:
                out.add(f"{float(v):,.0f}")
                out.add(f"{float(v):.1f}")
    return out


@pytest.mark.skipif(not LIVE, reason="set LENS_LIVE_EVAL=1 with a provider key to run the live golden set")
async def test_golden_live(login, allow, settings):
    """Acceptance: ≥ 90 % of fixtures produce runnable SQL first attempt; ≥ 85 % match the reference answer."""
    a = await login("analyst@lens.demo")
    sid = (await a.post("/sessions", json={})).json()["id"]
    runnable = matched = 0
    report = []
    guard = SqlGuard(settings)
    for case in GOLDEN:
        turn = (await a.post("/chat", json={"session_id": sid, "message": case["question"]})).json()
        ok = turn["status"] == "ok" and bool(turn["charts"])
        ref = await guard.check(case["reference_sql"], allow=allow, scope=WIDE, mode="refresh")
        ref_rows = await execute_readonly(ref.sql_bound, statement_timeout_ms=8000, max_rows=5000)
        match = False
        if ok:
            runnable += 1
            gen = await guard.check(turn["charts"][0]["sql_text"], allow=allow, scope=WIDE, mode="refresh")
            gen_rows = await execute_readonly(gen.sql_bound, statement_timeout_ms=8000, max_rows=5000)
            ref_keys = (
                _key_values(ref_rows.columns, ref_rows.rows, case["key_column"])
                if case["key_column"] in ref_rows.columns
                else set()
            )
            gen_keys = set()
            for col in gen_rows.columns:
                gen_keys |= _key_values(gen_rows.columns, gen_rows.rows, col)
            overlap = len(ref_keys & gen_keys) / max(1, len(ref_keys))
            narrative = turn["answer_markdown"] or ""
            figure_hit = any(f in narrative for f in _figures(ref_rows.columns, ref_rows.rows)) or any(
                k in narrative for k in list(ref_keys)[:5]
            )
            match = overlap >= 0.6 or figure_hit
            matched += int(match)
        report.append((case["id"], turn["status"], ok, match, turn.get("duration_ms")))
    for line in report:
        print(line)
    n = len(GOLDEN)
    assert runnable / n >= 0.9, f"runnable first attempt: {runnable}/{n}"
    assert matched / n >= 0.85, f"matching reference: {matched}/{n}"
