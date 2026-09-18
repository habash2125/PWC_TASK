"""Fixture SQL corpus for the guard.  ``BLOCKED`` must all be refused; ``ALLOWED`` must all pass (possibly repaired)."""

P = ":lens_scope_client_id"

BLOCKED: list[tuple[str, str, str]] = [
    # (id, sql, expected rule)
    ("ddl_drop", "DROP TABLE project", "read_only"),
    ("ddl_create", "CREATE TABLE x AS SELECT 1", "read_only"),
    ("dml_delete", "DELETE FROM project WHERE project_id = 1", "read_only"),
    ("dml_delete_view", "delete from v_project_overview", "read_only"),
    ("dml_update", "UPDATE project SET budget = 0", "read_only"),
    ("dml_insert", "INSERT INTO project VALUES (1)", "read_only"),
    ("truncate", "TRUNCATE project", "read_only"),
    (
        "multi_statement",
        f"SELECT client_name FROM v_project_overview WHERE client_id IN ({P}); DROP TABLE project",
        "single_statement",
    ),
    (
        "multi_statement_comment",
        f"SELECT client_name FROM v_project_overview WHERE client_id IN ({P}) -- x\n; SELECT 1",
        "single_statement",
    ),
    ("copy", "COPY project TO '/tmp/x'", "read_only"),
    ("set", "SET statement_timeout = 0", "read_only"),
    ("do_block", "DO $$ BEGIN PERFORM 1; END $$", "read_only|single_statement"),
    ("call", "CALL something()", "read_only"),
    ("grant", "GRANT SELECT ON project TO public", "read_only"),
    ("for_update", f"SELECT project_id FROM v_project_overview WHERE client_id IN ({P}) FOR UPDATE", "read_only"),
    ("pg_sleep", f"SELECT pg_sleep(10), client_id FROM v_project_overview WHERE client_id IN ({P})", "read_only"),
    ("pg_read_file", "SELECT pg_read_file('/etc/passwd')", "read_only"),
    ("pg_read_file_table", "SELECT * FROM pg_read_file('/etc/passwd')", "read_only"),
    ("dblink", "SELECT * FROM dblink('dbname=x', 'select 1') AS t(a int)", "read_only"),
    ("lo_import", "SELECT lo_import('/etc/passwd')", "read_only"),
    ("select_into", f"SELECT project_id INTO newtab FROM v_project_overview WHERE client_id IN ({P})", "read_only"),
    ("base_table", "SELECT * FROM project", "allow_list"),
    (
        "base_table_join",
        f"SELECT p.name FROM v_project_overview v JOIN project p ON p.project_id = v.project_id WHERE v.client_id IN ({P})",
        "allow_list",
    ),
    ("base_table_in_cte", "WITH x AS (SELECT * FROM employee) SELECT * FROM x", "allow_list"),
    (
        "base_table_in_subquery",
        f"SELECT client_id FROM v_project_overview WHERE client_id IN ({P}) AND project_id IN (SELECT project_id FROM timesheet_entry)",
        "allow_list",
    ),
    ("information_schema", "SELECT table_name FROM information_schema.tables", "allow_list"),
    ("pg_catalog", "SELECT relname FROM pg_catalog.pg_class", "allow_list"),
    ("pg_class_bare", "SELECT relname FROM pg_class", "allow_list"),
    ("other_schema", "SELECT * FROM other.v_project_overview", "allow_list"),
    ("disabled_or_unknown_view", "SELECT * FROM v_secret_view", "allow_list"),
    (
        "cte_shadowing_does_not_launder",
        "WITH v_project_overview AS (SELECT * FROM project) SELECT * FROM v_project_overview",
        "allow_list",
    ),
    (
        "sensitive_column",
        f"SELECT delivery_lead_email FROM v_project_overview WHERE client_id IN ({P})",
        "sensitive_column",
    ),
    ("sensitive_star", f"SELECT * FROM v_project_overview WHERE client_id IN ({P})", "sensitive_column"),
    ("sensitive_alias_star", f"SELECT p.* FROM v_project_overview p WHERE p.client_id IN ({P})", "sensitive_column"),
    (
        "sensitive_cost_rate",
        f"SELECT employee_name, cost_rate FROM v_utilisation_by_employee WHERE client_id IN ({P})",
        "sensitive_column",
    ),
    (
        "comment_hidden_statement",
        f"SELECT client_name FROM v_project_overview WHERE client_id IN ({P}) /* ; DROP TABLE project */ ; DROP TABLE project",
        "single_statement",
    ),
    ("unparseable", "SELEC client_name FROM v_project_overview", "parse"),
    ("explain", "EXPLAIN SELECT 1", "read_only"),
    ("lock_table", "LOCK TABLE project", "read_only|parse"),
    ("vacuum", "VACUUM project", "read_only"),
]

# Missing or mis-placed predicates: the deterministic guard REPAIRS these (never widens) and the parser records
# a shadow verdict; they must come out with verdict == "repaired" and the bound SQL must contain the literal scope.
REPAIRED: list[tuple[str, str]] = [
    ("missing_predicate", "SELECT client_name, budget FROM v_project_overview"),
    ("predicate_under_or", f"SELECT client_name FROM v_project_overview WHERE client_id IN ({P}) OR 1 = 1"),
    (
        "predicate_on_outer_only",
        f"SELECT * FROM (SELECT client_id, SUM(actual_spend) AS s FROM v_monthly_burn GROUP BY client_id) t WHERE t.client_id IN ({P})",
    ),
    (
        "predicate_missing_in_cte",
        f"WITH burn AS (SELECT project_id, SUM(actual_spend) AS spend FROM v_monthly_burn GROUP BY project_id) "
        f"SELECT p.project_name, b.spend FROM v_project_overview p JOIN burn b ON b.project_id = p.project_id WHERE p.client_id IN ({P})",
    ),
    (
        "predicate_on_wrong_alias",
        f"SELECT p.project_name FROM v_project_overview p JOIN v_project_milestones m ON m.project_id = p.project_id WHERE p.client_id IN ({P})",
    ),
    (
        "predicate_in_having",
        f"SELECT client_id, COUNT(*) FROM v_project_overview GROUP BY client_id HAVING client_id IN ({P})",
    ),
    (
        "union_branch_missing",
        f"SELECT client_name FROM v_project_overview WHERE client_id IN ({P}) UNION ALL SELECT client_name FROM v_revenue_by_client",
    ),
    (
        "right_join_on_does_not_filter",
        f"SELECT m.project_name FROM v_project_overview p RIGHT JOIN v_project_milestones m ON m.project_id = p.project_id AND m.client_id IN ({P}) WHERE p.client_id IN ({P})",
    ),
    (
        "literal_values_instead_of_placeholder",
        "SELECT client_name FROM v_project_overview WHERE client_id IN (1, 2, 3)",
    ),
]

ALLOWED: list[tuple[str, str]] = [
    ("simple", f"SELECT client_name, budget FROM v_project_overview WHERE client_id IN ({P})"),
    (
        "aliased",
        f"SELECT p.client_name, p.budget FROM v_project_overview AS p WHERE p.client_id IN ({P}) AND p.status = 'active'",
    ),
    (
        "cte",
        f"WITH burn AS (SELECT project_id, SUM(actual_spend) AS spend FROM v_monthly_burn WHERE client_id IN ({P}) GROUP BY project_id) "
        f"SELECT p.project_name, b.spend FROM v_project_overview p JOIN burn b ON b.project_id = p.project_id WHERE p.client_id IN ({P})",
    ),
    (
        "two_scoped_joins",
        f"SELECT p.project_name, m.milestone_completion_ratio FROM v_project_overview p JOIN v_project_milestones m "
        f"ON m.project_id = p.project_id WHERE p.client_id IN ({P}) AND m.client_id IN ({P}) AND p.budget_burn_ratio > 0.8",
    ),
    (
        "left_join_on_predicate",
        f"SELECT p.project_name, r.title FROM v_project_overview p LEFT JOIN v_risk_register r ON r.project_id = p.project_id "
        f"AND r.client_id IN ({P}) WHERE p.client_id IN ({P})",
    ),
    (
        "subquery_in_from",
        f"SELECT t.client_id, t.s FROM (SELECT client_id, SUM(actual_spend) AS s FROM v_monthly_burn WHERE client_id IN ({P}) GROUP BY client_id) t",
    ),
    (
        "union_both_branches",
        f"SELECT client_name FROM v_project_overview WHERE client_id IN ({P}) UNION ALL SELECT client_name FROM v_revenue_by_client WHERE client_id IN ({P})",
    ),
    (
        "window_and_order",
        f"SELECT project_name, month, actual_spend, SUM(actual_spend) OVER (PARTITION BY project_id ORDER BY month) AS cum "
        f"FROM v_monthly_burn WHERE client_id IN ({P}) ORDER BY month LIMIT 100",
    ),
    ("limit_above_cap_is_clamped", f"SELECT client_name FROM v_project_overview WHERE client_id IN ({P}) LIMIT 999999"),
    ("no_scoped_view", "SELECT 1 AS one"),
    ("trailing_semicolon", f"SELECT client_name FROM v_project_overview WHERE client_id IN ({P});"),
    (
        "column_named_like_keyword",
        f"SELECT status, COUNT(*) AS n FROM v_project_overview WHERE client_id IN ({P}) GROUP BY status",
    ),
]
