"""Pure unit tests for the deterministic parts of table selection: the lightweight catalog and the join graph.

Builds an ``AllowList`` directly from the real Northwind ``VIEWS`` catalogue (no DB round-trip), the same
shape ``load_allow_list`` produces.
"""

from __future__ import annotations

import uuid

from app.core.sql.schema_context import AllowList, ViewMeta
from app.core.sql.table_selector import build_relations, render_lightweight_catalog
from app.db.seed.analytics_catalog import VIEWS


def _allow_from_seed() -> AllowList:
    allow = AllowList(data_source_id=uuid.uuid4(), dialect="sqlite")
    for view in VIEWS:
        cols = [(c.name, c.type, c.description) for c in view.columns if not c.sensitive]
        sensitive = frozenset(c.name.lower() for c in view.columns if c.sensitive)
        allow.views[view.name.lower()] = ViewMeta(
            name=view.name,
            description=view.description,
            business_rules=view.business_rules,
            scope_column=view.scope_column,
            columns=tuple(cols),
            sensitive_columns=sensitive,
            allow_row_samples=view.allow_row_samples,
        )
    return allow


def test_relation_graph_links_orders_and_order_lines_on_shared_ids():
    allow = _allow_from_seed()
    rel = build_relations(allow)
    assert "v_order_lines <-> v_orders" in rel
    assert "order_id" in rel


def test_relation_graph_excludes_scope_column_from_every_pair():
    allow = _allow_from_seed()
    rel = build_relations(allow)
    assert "region_id" not in rel


def test_lightweight_catalog_has_no_business_rules_text():
    allow = _allow_from_seed()
    cat = render_lightweight_catalog(allow)
    assert "VIEW v_orders" in cat and "order_id" in cat
    assert "SUM(unit_price" not in cat  # business_rules text (v_orders) does not leak in
