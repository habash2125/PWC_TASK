"""Fixture SQL corpus for the guard.  ``BLOCKED`` must all be refused; ``ALLOWED`` must all pass (possibly repaired)."""

P = ":lens_scope_region_id"

BLOCKED: list[tuple[str, str, str]] = [
    # (id, sql, expected rule)
    ("ddl_drop", "DROP TABLE Products", "read_only"),
    ("ddl_create", "CREATE TABLE x AS SELECT 1", "read_only"),
    ("dml_delete", "DELETE FROM Products WHERE ProductID = 1", "read_only"),
    ("dml_delete_view", "delete from v_orders", "read_only"),
    ("dml_update", "UPDATE Products SET UnitsInStock = 0", "read_only"),
    ("dml_insert", "INSERT INTO Products VALUES (1)", "read_only"),
    ("dml_replace", "REPLACE INTO Products VALUES (1)", "read_only|parse"),
    ("truncate", "TRUNCATE Products", "read_only"),
    (
        "multi_statement",
        f"SELECT product_name FROM v_order_lines WHERE region_id IN ({P}); DROP TABLE Products",
        "single_statement",
    ),
    (
        "multi_statement_comment",
        f"SELECT product_name FROM v_order_lines WHERE region_id IN ({P}) -- x\n; SELECT 1",
        "single_statement",
    ),
    ("copy", "COPY Products TO '/tmp/x'", "read_only"),
    ("set", "SET statement_timeout = 0", "read_only"),
    ("do_block", "DO $$ BEGIN PERFORM 1; END $$", "read_only|single_statement"),
    ("call", "CALL something()", "read_only"),
    ("grant", "GRANT SELECT ON Products TO public", "read_only"),
    ("for_update", f"SELECT order_id FROM v_orders WHERE region_id IN ({P}) FOR UPDATE", "read_only"),
    ("pragma", "PRAGMA table_info(Products)", "read_only|parse"),
    (
        "pragma_in_select",
        f"SELECT order_id FROM v_orders WHERE region_id IN ({P}) AND pragma = 1",
        "read_only",
    ),
    ("attach", "ATTACH DATABASE '/tmp/x.db' AS y", "read_only|parse"),
    ("load_extension", "SELECT load_extension('/tmp/evil.so')", "read_only"),
    ("readfile", "SELECT readfile('/etc/passwd')", "read_only"),
    (
        "writefile",
        f"SELECT writefile('/tmp/x', product_name) FROM v_order_lines WHERE region_id IN ({P})",
        "read_only",
    ),
    ("pg_sleep", f"SELECT pg_sleep(10), region_id FROM v_orders WHERE region_id IN ({P})", "read_only"),
    ("pg_read_file", "SELECT pg_read_file('/etc/passwd')", "read_only"),
    ("dblink", "SELECT * FROM dblink('dbname=x', 'select 1') AS t(a int)", "read_only|parse"),
    ("select_into", f"SELECT order_id INTO newtab FROM v_orders WHERE region_id IN ({P})", "read_only"),
    ("base_table", "SELECT * FROM Products", "allow_list"),
    ("base_table_lowercase", "SELECT * FROM products", "allow_list"),
    ("base_table_with_space", 'SELECT * FROM "Order Details"', "allow_list"),
    (
        "base_table_join",
        f"SELECT p.ProductName FROM v_order_lines v JOIN Products p ON p.ProductID = v.product_id WHERE v.region_id IN ({P})",
        "allow_list",
    ),
    ("base_table_in_cte", "WITH x AS (SELECT * FROM Suppliers) SELECT * FROM x", "allow_list"),
    (
        "base_table_in_subquery",
        f"SELECT order_id FROM v_orders WHERE region_id IN ({P}) AND order_id IN (SELECT OrderID FROM Orders)",
        "allow_list",
    ),
    ("sqlite_master", "SELECT name FROM sqlite_master", "allow_list"),
    ("sqlite_schema", "SELECT sql FROM sqlite_schema", "allow_list"),
    ("main_sqlite_master", "SELECT name FROM main.sqlite_master", "allow_list"),
    ("information_schema", "SELECT table_name FROM information_schema.tables", "allow_list"),
    ("pg_catalog", "SELECT relname FROM pg_catalog.pg_class", "allow_list"),
    ("temp_schema", "SELECT * FROM temp.v_orders", "allow_list"),
    ("other_schema", "SELECT * FROM other.v_orders", "allow_list"),
    ("upstream_view_not_listed", "SELECT * FROM ProductDetails_V", "allow_list"),
    ("upstream_view_with_space", 'SELECT * FROM "Order Subtotals"', "allow_list"),
    ("disabled_or_unknown_view", "SELECT * FROM v_secret_view", "allow_list"),
    (
        "cte_shadowing_does_not_launder",
        "WITH v_orders AS (SELECT * FROM Orders) SELECT * FROM v_orders",
        "allow_list",
    ),
    ("sensitive_column", "SELECT customer_name, contact_name FROM v_customers", "sensitive_column"),
    ("sensitive_star", "SELECT * FROM v_customers", "sensitive_column"),
    ("sensitive_alias_star", "SELECT c.* FROM v_customers c", "sensitive_column"),
    ("sensitive_unscoped_view", "SELECT supplier_name, phone FROM v_suppliers", "sensitive_column"),
    ("sensitive_unscoped_star", "SELECT * FROM v_suppliers", "sensitive_column"),
    (
        "sensitive_via_join",
        f"SELECT o.order_id, c.phone FROM v_orders o JOIN v_customers c ON c.customer_id = o.customer_id WHERE o.region_id IN ({P})",
        "sensitive_column",
    ),
    (
        "comment_hidden_statement",
        f"SELECT product_name FROM v_order_lines WHERE region_id IN ({P}) /* ; DROP TABLE Products */ ; DROP TABLE Products",
        "single_statement",
    ),
    ("unparseable", "SELEC product_name FROM v_order_lines", "parse"),
    ("explain", "EXPLAIN SELECT 1", "read_only"),
    ("vacuum", "VACUUM", "read_only|parse"),
    ("reindex", "REINDEX Products", "read_only|parse"),
]

# Missing or mis-placed predicates: the deterministic guard REPAIRS these (never widens) and the parser records
# a shadow verdict; they must come out with verdict == "repaired" and the bound SQL must contain the literal scope.
REPAIRED: list[tuple[str, str]] = [
    ("missing_predicate", "SELECT employee_name, revenue FROM v_employee_sales"),
    ("predicate_under_or", f"SELECT employee_name FROM v_employee_sales WHERE region_id IN ({P}) OR 1 = 1"),
    (
        "predicate_on_outer_only",
        f"SELECT * FROM (SELECT region_id, SUM(revenue) AS s FROM v_monthly_sales GROUP BY region_id) t WHERE t.region_id IN ({P})",
    ),
    (
        "predicate_missing_in_cte",
        f"WITH cat AS (SELECT region_id, SUM(revenue) AS net FROM v_category_sales GROUP BY region_id) "
        f"SELECT e.employee_name, c.net FROM v_employee_sales e JOIN cat c ON c.region_id = e.region_id WHERE e.region_id IN ({P})",
    ),
    (
        "predicate_on_wrong_alias",
        f"SELECT e.employee_name FROM v_employee_sales e JOIN v_shipping_performance s ON s.region_id = e.region_id WHERE e.region_id IN ({P})",
    ),
    (
        "predicate_in_having",
        f"SELECT region_id, COUNT(*) FROM v_employee_sales GROUP BY region_id HAVING region_id IN ({P})",
    ),
    (
        "union_branch_missing",
        f"SELECT employee_name FROM v_employee_sales WHERE region_id IN ({P}) UNION ALL SELECT employee_name FROM v_monthly_sales",
    ),
    (
        "right_join_on_does_not_filter",
        f"SELECT s.shipper_name FROM v_employee_sales e RIGHT JOIN v_shipping_performance s ON s.region_id = e.region_id AND s.region_id IN ({P}) WHERE e.region_id IN ({P})",
    ),
    (
        "literal_values_instead_of_placeholder",
        "SELECT employee_name FROM v_employee_sales WHERE region_id IN (1, 2, 3)",
    ),
]

ALLOWED: list[tuple[str, str]] = [
    ("simple", f"SELECT product_name, quantity FROM v_order_lines WHERE region_id IN ({P})"),
    (
        "aliased",
        f"SELECT s.product_name, s.quantity FROM v_order_lines AS s WHERE s.region_id IN ({P}) AND s.discount > 0",
    ),
    (
        "cte",
        f"WITH cat AS (SELECT region_id, SUM(revenue) AS net FROM v_category_sales WHERE region_id IN ({P}) GROUP BY region_id) "
        f"SELECT e.employee_name, c.net FROM v_employee_sales e JOIN cat c ON c.region_id = e.region_id WHERE e.region_id IN ({P})",
    ),
    (
        "two_scoped_joins",
        f"SELECT e.employee_name, m.order_month, m.revenue FROM v_employee_sales e JOIN v_monthly_sales m "
        f"ON m.employee_id = e.employee_id AND m.region_id = e.region_id WHERE e.region_id IN ({P}) AND m.region_id IN ({P}) AND m.revenue > 100000",
    ),
    (
        "left_join_on_predicate",
        f"SELECT e.employee_name, s.shipper_name FROM v_employee_sales e LEFT JOIN v_shipping_performance s ON s.region_id = e.region_id "
        f"AND s.region_id IN ({P}) WHERE e.region_id IN ({P})",
    ),
    (
        "subquery_in_from",
        f"SELECT t.region_id, t.s FROM (SELECT region_id, SUM(revenue) AS s FROM v_monthly_sales WHERE region_id IN ({P}) GROUP BY region_id) t",
    ),
    (
        "union_both_branches",
        f"SELECT employee_name FROM v_employee_sales WHERE region_id IN ({P}) UNION ALL SELECT employee_name FROM v_monthly_sales WHERE region_id IN ({P})",
    ),
    (
        "window_and_order",
        f"SELECT employee_name, order_month, revenue, SUM(revenue) OVER (PARTITION BY employee_id ORDER BY order_month) AS running "
        f"FROM v_monthly_sales WHERE region_id IN ({P}) ORDER BY order_month LIMIT 100",
    ),
    (
        "limit_above_cap_is_clamped",
        f"SELECT order_id FROM v_orders WHERE region_id IN ({P}) LIMIT 999999",
    ),
    ("no_scoped_view", "SELECT 1 AS one"),
    ("unscoped_reference_view", "SELECT product_id, product_name, unit_price FROM v_products ORDER BY unit_price DESC"),
    (
        "unscoped_joined_to_scoped",
        f"SELECT c.category_name, SUM(s.revenue) AS v FROM v_category_sales s JOIN v_products c ON c.category_id = s.category_id "
        f"WHERE s.region_id IN ({P}) GROUP BY c.category_name",
    ),
    ("trailing_semicolon", f"SELECT order_id FROM v_orders WHERE region_id IN ({P});"),
    (
        "column_named_like_keyword",
        f"SELECT order_year, COUNT(*) AS n FROM v_orders WHERE region_id IN ({P}) GROUP BY order_year",
    ),
    (
        "sqlite_date_functions",
        f"SELECT order_date, SUM(order_total) AS revenue FROM v_orders WHERE region_id IN ({P}) "
        f"AND order_date >= date('2023-10-28', '-30 days') GROUP BY order_date ORDER BY order_date",
    ),
    (
        "strftime_month_bucket",
        f"SELECT strftime('%Y-%m', order_date) AS month, SUM(order_total) AS revenue FROM v_orders "
        f"WHERE region_id IN ({P}) AND is_shipped = 1 GROUP BY strftime('%Y-%m', order_date) ORDER BY month",
    ),
    (
        "case_in_aggregate",
        f"SELECT region_name, SUM(CASE WHEN shipped_late = 1 THEN 1 ELSE 0 END) AS late, COUNT(*) AS total "
        f"FROM v_orders WHERE region_id IN ({P}) GROUP BY region_name",
    ),
]
