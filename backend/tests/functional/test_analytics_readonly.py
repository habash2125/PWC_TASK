"""Phase 3 gate: the read-only role can select from views and nothing else."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.analytics_pool import execute_readonly, get_analytics_engine
from app.db.seed.analytics_catalog import VIEW_NAMES


async def test_readonly_role_can_select_every_view(app):
    for view in VIEW_NAMES:
        result = await execute_readonly(f"SELECT * FROM {view} LIMIT 3", statement_timeout_ms=5000, max_rows=10)
        assert result.columns, view


@pytest.mark.parametrize("table", ["project", "client", "employee", "timesheet_entry", "invoice", "risk"])
async def test_readonly_role_is_denied_on_base_tables(app, table: str):
    engine = get_analytics_engine()
    with pytest.raises(DBAPIError) as exc:
        async with engine.connect() as conn:
            await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
    assert "permission denied" in str(exc.value).lower()


async def test_readonly_role_cannot_write_even_through_views(app):
    engine = get_analytics_engine()
    with pytest.raises(DBAPIError):
        async with engine.connect() as conn:
            await conn.execute(text("CREATE TABLE should_not_exist (id int)"))
    with pytest.raises(DBAPIError):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM project"))


async def test_statement_timeout_is_enforced(app):
    from app.db.analytics_pool import AnalyticsQueryError

    with pytest.raises(AnalyticsQueryError) as exc:
        await execute_readonly(
            "SELECT COUNT(*) FROM v_utilisation_by_employee a, v_utilisation_by_employee b, v_utilisation_by_employee c",
            statement_timeout_ms=200,
            max_rows=10,
        )
    assert exc.value.code == "timeout"
