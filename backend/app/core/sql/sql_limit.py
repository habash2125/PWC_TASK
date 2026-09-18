"""Row cap: every executed statement carries an outer LIMIT of at most ``SQL_MAX_ROWS``."""

from __future__ import annotations

from sqlglot import exp


def apply_row_cap(root: exp.Expression, max_rows: int) -> tuple[exp.Expression, bool]:
    """Returns ``(expression, changed)``.  Clamps an existing LIMIT, adds one when absent, wraps set operations."""
    if isinstance(root, exp.Select):
        limit = root.args.get("limit")
        if limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.is_int:
            current = int(limit.expression.this)
            if current <= max_rows:
                return root, False
        root.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return root, True
    if isinstance(root, (exp.Union, exp.Intersect, exp.Except)):
        limit = root.args.get("limit")
        if (
            limit is not None
            and isinstance(limit.expression, exp.Literal)
            and limit.expression.is_int
            and int(limit.expression.this) <= max_rows
        ):
            return root, False
        root.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return root, True
    raise ValueError(f"cannot cap a {type(root).__name__}")
