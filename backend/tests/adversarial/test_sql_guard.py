"""Phase 6 gate: the adversarial corpus against the real guard — no model involved, zero false negatives."""

from __future__ import annotations

import pytest
import sqlglot

from app.core.security.access_scope import ScopePredicate, bind_scope, find_scoped_references, parse_one, repair_scope
from app.core.sql.schema_context import load_allow_list
from app.core.sql.sql_guard import SqlGuard
from app.db.analytics_pool import execute_readonly
from tests.adversarial.sql_corpus import ALLOWED, BLOCKED, REPAIRED

NARROW = ScopePredicate(scope_key="client_id", values=(1, 2), unrestricted=False)
WIDE = ScopePredicate(scope_key="client_id", values=(), unrestricted=True)
NONE = ScopePredicate(scope_key="client_id", values=(), unrestricted=False)


@pytest.fixture(scope="module")
async def allow(app, seeded):
    from app.db.session import session_factory

    async with session_factory()() as s:
        return await load_allow_list(s, seeded["source_id"])


@pytest.fixture(scope="module")
def guard(settings):
    return SqlGuard(settings)


@pytest.mark.parametrize("case_id,sql,rule", BLOCKED, ids=[c[0] for c in BLOCKED])
async def test_blocked_corpus(guard, allow, case_id, sql, rule):
    result = await guard.check(sql, allow=allow, scope=NARROW, mode="refresh")
    assert result.blocked, f"{case_id}: expected block, got {result.verdict} ({result.reason})"
    assert set(rule.split("|")) & set(result.rules_failed), (
        f"{case_id}: expected rule {rule}, got {result.rules_failed}: {result.reason}"
    )
    assert result.sql_bound is None
    assert not result.llm_used


@pytest.mark.parametrize("case_id,sql", REPAIRED, ids=[c[0] for c in REPAIRED])
async def test_missing_or_misplaced_predicates_are_repaired_never_widened(guard, allow, case_id, sql):
    result = await guard.check(sql, allow=allow, scope=NARROW, mode="refresh")
    assert result.verdict == "repaired", f"{case_id}: {result.verdict} {result.reason}"
    assert result.shadow_parser_verdict and result.shadow_parser_verdict.startswith("repaired")
    assert result.sql_bound and "client_id IN (1, 2)" in result.sql_bound
    # every scoped reference in the executed SQL is covered, in its own SELECT
    check = find_scoped_references(parse_one(result.sql_canonical), allow.scope_columns)
    assert check.ok and check.references
    # ...and the bound SQL actually runs against the read-only role
    res = await execute_readonly(result.sql_bound, statement_timeout_ms=5000, max_rows=50)
    assert res.columns


@pytest.mark.parametrize("case_id,sql", ALLOWED, ids=[c[0] for c in ALLOWED])
async def test_allowed_corpus(guard, allow, case_id, sql):
    result = await guard.check(sql, allow=allow, scope=NARROW, mode="refresh")
    assert not result.blocked, f"{case_id}: {result.reason}"
    assert result.verdict == "allowed", f"{case_id}: unexpected repair: {result.reason}"
    assert result.sql_bound and result.sql_canonical and result.sql_hash
    assert (
        ":lens_scope" not in result.sql_bound and ":lens_scope" in result.sql_canonical or case_id == "no_scoped_view"
    )
    res = await execute_readonly(result.sql_bound, statement_timeout_ms=5000, max_rows=5000)
    assert res.columns


async def test_row_cap_is_always_present(guard, allow, settings):
    r = await guard.check(
        "SELECT client_name FROM v_project_overview WHERE client_id IN (:lens_scope_client_id)",
        allow=allow,
        scope=WIDE,
        mode="refresh",
    )
    assert r.row_cap_applied and f"LIMIT {settings.sql_max_rows}" in r.sql_bound
    r = await guard.check(
        "SELECT client_name FROM v_project_overview WHERE client_id IN (:lens_scope_client_id) LIMIT 999999",
        allow=allow,
        scope=WIDE,
        mode="refresh",
    )
    assert f"LIMIT {settings.sql_max_rows}" in r.sql_bound and "999999" not in r.sql_bound
    r = await guard.check(
        "SELECT client_name FROM v_project_overview WHERE client_id IN (:lens_scope_client_id) LIMIT 10",
        allow=allow,
        scope=WIDE,
        mode="refresh",
    )
    assert not r.row_cap_applied and "LIMIT 10" in r.sql_bound


async def test_scope_binding_per_executor(guard, allow):
    sql = "SELECT client_id, client_name FROM v_project_overview WHERE client_id IN (:lens_scope_client_id)"
    narrow = await guard.check(sql, allow=allow, scope=NARROW, mode="refresh")
    wide = await guard.check(sql, allow=allow, scope=WIDE, mode="refresh")
    assert narrow.sql_hash == wide.sql_hash  # same canonical query, different binding
    assert "IN (1, 2)" in narrow.sql_bound and "TRUE" in wide.sql_bound
    rows_narrow = await execute_readonly(narrow.sql_bound, statement_timeout_ms=5000, max_rows=5000)
    rows_wide = await execute_readonly(wide.sql_bound, statement_timeout_ms=5000, max_rows=5000)
    assert {r[0] for r in rows_narrow.rows} <= {1, 2}
    assert len(rows_wide.rows) > len(rows_narrow.rows)
    # a principal with no scope row is blocked before any binding
    none = await guard.check(sql, allow=allow, scope=NONE, mode="refresh")
    assert none.blocked and none.kind == "scope"


async def test_string_scope_values_are_quoted_literals(allow):
    root = parse_one("SELECT region FROM v_project_overview WHERE region IN (:lens_scope_region)")
    bound = bind_scope(root, ScopePredicate("region", ("UK", "E'MEA")))
    assert "'UK'" in bound and "E''MEA" in bound  # quotes escaped, never interpolated raw


async def test_unbound_placeholder_is_refused(allow):
    root = parse_one("SELECT region FROM v_project_overview WHERE region IN (:lens_scope_other)")
    with pytest.raises(ValueError):
        bind_scope(root, NARROW)


async def test_repair_targets_the_reading_select_not_the_outer(allow):
    sql = "SELECT * FROM (SELECT client_id, SUM(actual_spend) AS s FROM v_monthly_burn GROUP BY client_id) t"
    repaired, n = repair_scope(parse_one(sql), allow.scope_columns)
    assert n == 1
    text = repaired.sql(dialect="postgres")
    inner = sqlglot.parse_one(text, read="postgres").find(sqlglot.exp.Subquery).this
    assert "lens_scope_client_id" in inner.args["where"].sql()


async def test_zero_queries_reach_executor_without_verified_scope(guard, allow, monkeypatch):
    """Instrument the executor: every statement the guard emits carries the bound scope predicate."""
    seen: list[str] = []
    from app.db import analytics_pool

    original = analytics_pool.execute_readonly

    async def spy(sql, **kw):
        seen.append(sql)
        return await original(sql, **kw)

    monkeypatch.setattr(analytics_pool, "execute_readonly", spy)
    corpus = [s for _, s in ALLOWED + REPAIRED if "v_" in s]
    for sql in corpus:
        r = await guard.check(sql, allow=allow, scope=NARROW, mode="refresh")
        assert not r.blocked
        await analytics_pool.execute_readonly(r.sql_bound, statement_timeout_ms=5000, max_rows=10)
    assert len(seen) == len(corpus)
    for executed in seen:
        assert ":lens_scope" not in executed
        assert "client_id IN (1, 2)" in executed
