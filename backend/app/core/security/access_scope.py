"""Row-level access scope: fetch (fail-closed), verify placement, repair, bind.

The convention the model is given
---------------------------------
Every SELECT that reads a scoped view must carry, in its own WHERE (or in the
ON clause of the join that introduces the view), the predicate::

    <alias>.<scope_column> IN (:lens_scope_<scope_column>)

The placeholder keeps stored SQL *scope-agnostic*: a pinned chart carries the
structure of the filter, and ``bind`` fills in the values of **whoever executes
it** — the viewer on a refresh, never the pinner.  Unrestricted principals bind
the predicate to ``TRUE``.

Why the placement rule matters: a predicate bolted onto the outer WHERE of a
query that pre-aggregates or LEFT JOINs a scoped view has already leaked rows
into the aggregate.  The verifier therefore requires the predicate *inside* the
SELECT that reads the view.

Fail-closed on every path: no scope row means no access; an unavailable scope
source, a timeout, or an unrepairable placement all block execution.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

import sqlglot
from sqlglot import exp

from app.api.errors import ScopeUnavailable
from app.observability import metrics

log = logging.getLogger("lens.sql")

PLACEHOLDER_PREFIX = "lens_scope_"


@dataclass(frozen=True, slots=True)
class ScopePredicate:
    scope_key: str
    values: tuple[int | str, ...]
    unrestricted: bool = False

    @property
    def has_access(self) -> bool:
        return self.unrestricted or len(self.values) > 0

    @property
    def placeholder(self) -> str:
        return f"{PLACEHOLDER_PREFIX}{self.scope_key}"

    def describe(self) -> str:
        if self.unrestricted:
            return f"{self.scope_key}: unrestricted"
        return (
            f"{self.scope_key} IN ({', '.join(map(str, self.values))})"
            if self.values
            else f"{self.scope_key}: no access"
        )


class ScopeSource(Protocol):
    """Where scopes come from.  A database table today; an external authorisation service tomorrow."""

    async def fetch(self, user_id: uuid.UUID, data_source_id: uuid.UUID, scope_key: str) -> ScopePredicate: ...


class DbScopeSource:
    def __init__(self, session_factory, timeout_seconds: float = 2.0):
        self.session_factory = session_factory
        self.timeout = timeout_seconds

    async def fetch(self, user_id: uuid.UUID, data_source_id: uuid.UUID, scope_key: str) -> ScopePredicate:
        from app.db.repos.users import UserRepo

        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.timeout):
                async with self.session_factory() as session:
                    rows = await UserRepo(session).scopes_for(user_id, data_source_id)
        except TimeoutError:
            metrics.stage_latency.labels("scope").observe(time.perf_counter() - started)
            log.error("scope fetch timed out", extra={"user_id": str(user_id)})
            raise ScopeUnavailable() from None
        except Exception as exc:
            metrics.stage_latency.labels("scope").observe(time.perf_counter() - started)
            log.error("scope fetch failed", extra={"user_id": str(user_id), "error": type(exc).__name__})
            raise ScopeUnavailable() from None
        metrics.stage_latency.labels("scope").observe(time.perf_counter() - started)
        for row in rows:
            if row.scope_key == scope_key:
                values = list(row.scope_values or [])
                if "*" in values:
                    return ScopePredicate(scope_key=scope_key, values=(), unrestricted=True)
                clean: list[int | str] = []
                for v in values:
                    if isinstance(v, bool) or not isinstance(v, (int, str)):
                        continue
                    clean.append(v)
                return ScopePredicate(scope_key=scope_key, values=tuple(clean), unrestricted=False)
        # absence of a row is NO access, not unrestricted access
        return ScopePredicate(scope_key=scope_key, values=(), unrestricted=False)


# ── AST helpers ──────────────────────────────────────────────────────────────


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    """Top-level AND conjuncts.  Anything under OR/NOT is not a restricting predicate."""
    if node is None:
        return []
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _is_scope_predicate(node: exp.Expression, qualifier: str, scope_column: str) -> bool:
    if not isinstance(node, exp.In):
        return False
    col = node.this
    if not isinstance(col, exp.Column) or col.name.lower() != scope_column.lower():
        return False
    table = (col.table or "").lower()
    if table and qualifier and table != qualifier.lower():
        return False
    exprs = node.expressions
    if len(exprs) != 1 or not isinstance(exprs[0], exp.Placeholder):
        return False
    return str(exprs[0].this).lower() == f"{PLACEHOLDER_PREFIX}{scope_column}".lower()


@dataclass(slots=True)
class ScopedReference:
    table: exp.Table
    select: exp.Select
    qualifier: str
    scope_column: str
    covered: bool = False
    where: str = ""


@dataclass(slots=True)
class ScopeCheck:
    references: list[ScopedReference] = field(default_factory=list)

    @property
    def violations(self) -> list[ScopedReference]:
        return [r for r in self.references if not r.covered]

    @property
    def ok(self) -> bool:
        return not self.violations


def find_scoped_references(root: exp.Expression, scope_columns: dict[str, str]) -> ScopeCheck:
    """Locates every reference to a scoped view and whether its own SELECT filters it.

    ``scope_columns`` maps view name → scope column.
    """
    check = ScopeCheck()
    for table in root.find_all(exp.Table):
        name = table.name.lower()
        if name not in scope_columns:
            continue
        scope_column = scope_columns[name]
        select = table.find_ancestor(exp.Select)
        if select is None:
            check.references.append(ScopedReference(table, None, table.alias_or_name, scope_column))  # type: ignore[arg-type]
            continue
        qualifier = table.alias_or_name
        ref = ScopedReference(table=table, select=select, qualifier=qualifier, scope_column=scope_column)
        # WHERE conjuncts of the reading SELECT
        where = select.args.get("where")
        if any(_is_scope_predicate(c, qualifier, scope_column) for c in _conjuncts(where.this if where else None)):
            ref.covered, ref.where = True, "where"
        else:
            # ON conjuncts of the join that introduces this table (inner/left joins filter the joined side)
            join = table.find_ancestor(exp.Join)
            if join is not None and join.this is table and (join.side or "").upper() not in ("RIGHT", "FULL"):
                if any(_is_scope_predicate(c, qualifier, scope_column) for c in _conjuncts(join.args.get("on"))):
                    ref.covered, ref.where = True, "on"
        check.references.append(ref)
    return check


def repair_scope(root: exp.Expression, scope_columns: dict[str, str]) -> tuple[exp.Expression, int]:
    """Injects the missing predicate into the SELECT that reads each uncovered scoped view.

    Only ever narrows the result; returns the number of injected predicates.
    """
    injected = 0
    check = find_scoped_references(root, scope_columns)
    for ref in check.violations:
        if ref.select is None:
            raise ValueError(f"cannot place a scope predicate for {ref.table.sql()}: no enclosing SELECT")
        predicate = exp.In(
            this=exp.column(ref.scope_column, table=ref.qualifier),
            expressions=[exp.Placeholder(this=f"{PLACEHOLDER_PREFIX}{ref.scope_column}")],
        )
        ref.select.where(predicate, append=True, copy=False)
        injected += 1
    return root, injected


def bind_scope(root: exp.Expression, predicate: ScopePredicate, dialect: str = "postgres") -> str:
    """Replaces every ``IN (:lens_scope_<key>)`` with the executor's literal values.

    * unrestricted → ``TRUE``
    * no access    → ``FALSE`` (the query legally returns nothing; callers block earlier anyway)
    """
    placeholder = predicate.placeholder.lower()
    bound = 0

    def _transform(node: exp.Expression) -> exp.Expression:
        nonlocal bound
        if isinstance(node, exp.In) and len(node.expressions) == 1 and isinstance(node.expressions[0], exp.Placeholder):
            if str(node.expressions[0].this).lower() == placeholder:
                bound += 1
                if predicate.unrestricted:
                    return exp.true()
                if not predicate.values:
                    return exp.false()
                literals = [
                    exp.Literal.number(v) if isinstance(v, int) else exp.Literal.string(str(v))
                    for v in predicate.values
                ]
                return exp.In(this=node.this.copy(), expressions=literals)
        return node

    result = root.transform(_transform, copy=True)
    leftovers = [p for p in result.find_all(exp.Placeholder)]
    if leftovers:
        raise ValueError(f"unbound placeholder(s) after scope binding: {[p.sql() for p in leftovers]}")
    return result.sql(dialect=dialect)


def scope_prompt_instruction(predicate: ScopePredicate, scoped_views: list[str]) -> str:
    """The mandatory-predicate instruction injected into the generation prompt (as data, not policy)."""
    col = predicate.scope_key
    views = ", ".join(scoped_views)
    lines = [
        f"ROW-LEVEL SCOPE (mandatory): every SELECT that reads any of these views — {views} — must include, "
        f"in that SELECT's own WHERE clause, the predicate `<alias>.{col} IN (:{PLACEHOLDER_PREFIX}{col})`.",
        "Use exactly that placeholder; the server binds the caller's allowed values at execution time. "
        "Put it inside CTEs and subqueries that read the view, not only on the outer query.",
    ]
    if predicate.unrestricted:
        lines.append("This caller currently has unrestricted access, but the placeholder is still required.")
    else:
        lines.append(f"This caller may only see {col} values in: {', '.join(map(str, predicate.values))}.")
    return "\n".join(lines)


def parse_one(sql: str, dialect: str = "postgres") -> exp.Expression:
    return sqlglot.parse_one(sql, read=dialect)
