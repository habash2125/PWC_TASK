"""Phase 3 gate: the analytics connection can select from the allow-listed views and nothing else.

SQLite has no roles; the guarantee comes from ``mode=ro`` + ``PRAGMA query_only`` + the authorizer
installed on every connection (see ``app.db.analytics_pool``).  These tests bypass the SQL guard on
purpose: they prove the database itself refuses what the guard would have refused.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.analytics_pool import AnalyticsQueryError, execute_readonly, get_analytics_engine
from app.db.seed.analytics_catalog import VIEW_NAMES
from app.db.seed.analytics_seed import BASE_TABLES


async def test_readonly_connection_can_select_every_view(app):
    for view in VIEW_NAMES:
        result = await execute_readonly(f"SELECT * FROM {view} LIMIT 3", statement_timeout_ms=5000, max_rows=10)
        assert result.columns, view


async def test_views_can_be_joined_and_aggregated(app):
    result = await execute_readonly(
        "WITH s AS (SELECT region_id, SUM(revenue) AS v FROM v_monthly_sales GROUP BY region_id) "
        "SELECT e.region_name, s.v FROM v_employee_sales e JOIN s ON s.region_id = e.region_id "
        "GROUP BY e.region_name, s.v ORDER BY s.v DESC",
        statement_timeout_ms=5000,
        max_rows=10,
    )
    assert result.row_count == 4


@pytest.mark.parametrize("table", BASE_TABLES)
async def test_base_tables_are_denied(app, table: str):
    engine = get_analytics_engine()
    with pytest.raises(DBAPIError) as exc:
        async with engine.connect() as conn:
            await conn.execute(text(f'SELECT COUNT(*) FROM "{table}"'))
    assert "not authorized" in str(exc.value).lower() or "prohibited" in str(exc.value).lower()


async def test_base_table_columns_are_denied_even_when_joined_to_a_view(app):
    with pytest.raises(AnalyticsQueryError) as exc:
        await execute_readonly(
            "SELECT p.ProductName FROM v_products v JOIN Products p ON p.ProductID = v.product_id",
            statement_timeout_ms=5000,
            max_rows=10,
        )
    assert exc.value.code == "denied"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name FROM sqlite_master",
        "PRAGMA table_info(Products)",
        'SELECT * FROM "Order Subtotals"',  # exists in the file (upstream view) but is not on the allow-list
        "ATTACH DATABASE ':memory:' AS x",
        "SELECT load_extension('x')",
    ],
)
async def test_catalogue_pragma_attach_and_unlisted_views_are_denied(app, sql: str):
    engine = get_analytics_engine()
    with pytest.raises(DBAPIError):
        async with engine.connect() as conn:
            await conn.execute(text(sql))


async def test_connection_cannot_write(app):
    engine = get_analytics_engine()
    with pytest.raises(DBAPIError):
        async with engine.connect() as conn:
            await conn.execute(text("CREATE TABLE should_not_exist (id int)"))
    with pytest.raises(DBAPIError):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM Products"))
    with pytest.raises(DBAPIError):
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE Products SET UnitsInStock = 0"))


async def test_statement_timeout_is_enforced(app):
    with pytest.raises(AnalyticsQueryError) as exc:
        await execute_readonly(
            "SELECT COUNT(*) FROM v_products a, v_products b, v_products c, v_products d",
            statement_timeout_ms=200,
            max_rows=10,
        )
    assert exc.value.code == "timeout"


async def test_row_cap_marks_truncation(app):
    result = await execute_readonly("SELECT product_id FROM v_products", statement_timeout_ms=5000, max_rows=5)
    assert result.row_count == 5 and result.truncated
