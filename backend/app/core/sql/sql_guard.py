"""The SQL guard: every generated statement passes through here before execution.

Rules (Section 8.2)
-------------------
1. exactly one statement                                  — deterministic, hard
2. read-only SELECT / WITH…SELECT, deny-listed functions   — deterministic, hard
3. every table resolves to the enabled allow-list or a CTE — deterministic, hard
4. row cap                                                 — deterministic, hard
5. scope predicate present and correctly placed            — LLM verifier enforces in chat mode,
                                                             parser records a shadow verdict;
                                                             parser enforces on refresh (no LLM)
6. statement timeout                                       — applied by the executor

After the verdict, deterministic code *always* repairs any missing scope
predicate and binds the executor's values.  The model can veto; it can never
widen.  That is the sense in which the model is "never on the authorisation
path": whatever it says, the SQL that reaches the database carries the scope
predicate that code put there.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from app.config import Settings
from app.core.security.access_scope import (
    ScopePredicate,
    bind_scope,
    find_scoped_references,
    repair_scope,
)
from app.core.sql.normalise import sql_hash
from app.core.sql.schema_context import AllowList
from app.core.sql.sql_limit import apply_row_cap
from app.observability import metrics

log = logging.getLogger("lens.sql")

Verdict = Literal["allowed", "repaired", "blocked"]

FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,
    exp.Copy,
    exp.Grant,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Lock,
    exp.Into,
    exp.Comment,
)
FORBIDDEN_FUNCTIONS = {
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "dblink",
    "dblink_connect",
    "dblink_exec",
    "lo_import",
    "lo_export",
    "lo_get",
    "lo_put",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "set_config",
    "current_setting",
    "pg_notify",
    "query_to_xml",
    "pg_advisory_lock",
    "pg_try_advisory_lock",
    "pg_logical_slot_get_changes",
    "pg_get_functiondef",
}
FORBIDDEN_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}
# belt and braces on the raw text (comments stripped): catches keywords sqlglot may fold into a Command
_RAW_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|copy|call|do|vacuum|analyze|reindex|"
    r"cluster|listen|notify|prepare|execute|deallocate|discard|lock|refresh\s+materialized|security\s+label|comment\s+on)\b"
    r"|\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b|\bpg_sleep\b|\bpg_read_file\b|\bdblink\b|\blo_import\b",
    re.IGNORECASE,
)
_COMMENT = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.DOTALL)


class GuardVerdict(BaseModel):
    """Structured output of the LLM verifier — never scraped from free text."""

    model_config = ConfigDict(extra="forbid")
    compliant: bool
    repaired_sql: str | None = Field(
        default=None, description="A corrected statement when the original was not compliant and a safe fix exists"
    )
    reason: str = Field(max_length=600)


class GuardBlockedError(Exception):
    def __init__(self, reason: str, *, rule: str):
        super().__init__(reason)
        self.reason = reason
        self.rule = rule


@dataclass(slots=True)
class GuardResult:
    verdict: Verdict
    reason: str
    kind: Literal["sql_guard", "scope"] = "sql_guard"
    sql_canonical: str | None = None  # scope-agnostic, placeholders intact — what gets stored and hashed
    sql_bound: str | None = None  # executable: predicate bound to the executor's values, row cap applied
    sql_hash: str | None = None
    rules_failed: list[str] = field(default_factory=list)
    shadow_parser_verdict: str | None = None
    llm_used: bool = False
    scope_injected: int = 0
    row_cap_applied: bool = False

    @property
    def blocked(self) -> bool:
        return self.verdict == "blocked"


# ── deterministic rules ──────────────────────────────────────────────────────


def strip_comments(sql: str) -> str:
    return _COMMENT.sub(" ", sql)


def parse_single(sql: str, dialect: str = "postgres") -> exp.Expression:
    """Rule 1: exactly one statement."""
    cleaned = strip_comments(sql).strip()
    if not cleaned:
        raise GuardBlockedError("empty statement", rule="single_statement")
    # a trailing semicolon is tolerated; anything after it is not
    body = cleaned.rstrip(";").strip()
    if ";" in body:
        raise GuardBlockedError("multiple statements are not allowed", rule="single_statement")
    try:
        statements = [
            s for s in sqlglot.parse(body, read=dialect) if s is not None and not isinstance(s, exp.Semicolon)
        ]
    except sqlglot.errors.ParseError as exc:
        raise GuardBlockedError(f"SQL does not parse: {str(exc).splitlines()[0][:200]}", rule="parse") from None
    if len(statements) != 1:
        raise GuardBlockedError("exactly one statement is required", rule="single_statement")
    return statements[0]


def check_read_only(root: exp.Expression, raw_sql: str) -> None:
    """Rule 2."""
    inner = root.this if isinstance(root, exp.With) else root
    if not isinstance(inner, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise GuardBlockedError(f"only SELECT statements are allowed (got {type(inner).__name__})", rule="read_only")
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise GuardBlockedError(f"forbidden construct: {type(node).__name__}", rule="read_only")
        if isinstance(node, (exp.Anonymous, exp.Func)):
            name = (node.name or node.sql_name() or "").lower()
            if name in FORBIDDEN_FUNCTIONS:
                raise GuardBlockedError(f"forbidden function: {name}", rule="read_only")
    m = _RAW_FORBIDDEN.search(strip_comments(raw_sql))
    if m:
        # the parser may have accepted it as an identifier/alias; be conservative only if it is not quoted
        token = m.group(0)
        if not re.search(rf'"[^"]*{re.escape(token)}[^"]*"|\'[^\']*{re.escape(token)}[^\']*\'', raw_sql, re.IGNORECASE):
            raise GuardBlockedError(f"forbidden keyword: {token.strip().lower()}", rule="read_only")


def _cte_names(root: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}


def _derived_aliases(root: exp.Expression) -> set[str]:
    return {sq.alias_or_name.lower() for sq in root.find_all(exp.Subquery) if sq.alias_or_name}


def check_allow_list(root: exp.Expression, allow: AllowList) -> None:
    """Rule 3 plus the sensitive-column surface."""
    ctes = _cte_names(root)
    derived = _derived_aliases(root)
    allowed = allow.names
    referenced_views: set[str] = set()
    alias_to_view: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise GuardBlockedError(f"table functions are not allowed: {table.sql()[:80]}", rule="allow_list")
        schema = (table.db or "").lower()
        if schema and schema in FORBIDDEN_SCHEMAS:
            raise GuardBlockedError(f"schema {schema} is not accessible", rule="allow_list")
        if schema and schema != "public":
            raise GuardBlockedError(f"schema {schema} is not on the allow-list", rule="allow_list")
        name = table.name.lower()
        if name in ctes or name in derived:
            continue
        if name not in allowed:
            raise GuardBlockedError(f"'{table.name}' is not an allow-listed view", rule="allow_list")
        referenced_views.add(name)
        alias_to_view[table.alias_or_name.lower()] = name
    sensitive = {c for v in referenced_views for c in allow.views[v].sensitive_columns}
    if not sensitive:
        return
    sensitive_views = {v for v in referenced_views if allow.views[v].sensitive_columns}
    for col in root.find_all(exp.Column):
        if col.name.lower() in sensitive:
            raise GuardBlockedError(f"column '{col.name}' is not exposed", rule="sensitive_column")
    for star in root.find_all(exp.Star):
        parent = star.parent
        # only projection stars matter: `SELECT *` (parent Select) or `alias.*` (parent Column); COUNT(*) is not a projection
        if isinstance(parent, exp.Column):
            qualifier = (parent.table or "").lower()
            if qualifier and alias_to_view.get(qualifier) in sensitive_views:
                raise GuardBlockedError(
                    f"'{qualifier}.*' is not permitted: list columns explicitly", rule="sensitive_column"
                )
            if not qualifier and parent.parent is not None and isinstance(parent.parent, exp.Select):
                parent = parent.parent
            else:
                continue
        if isinstance(parent, exp.Select):
            select = parent
            if select is not None:
                for t in select.find_all(exp.Table):
                    if t.find_ancestor(exp.Select) is select and t.name.lower() in sensitive_views:
                        raise GuardBlockedError(
                            f"'SELECT *' is not permitted on {t.name}: list columns explicitly", rule="sensitive_column"
                        )


# ── the LLM verifier ─────────────────────────────────────────────────────────


async def llm_verify(sql: str, allow: AllowList, scope: ScopePredicate) -> tuple[GuardVerdict, str]:
    """Asks the guard model for a structured verdict.  Returns ``(verdict, prompt_version_id)``."""
    from app.core.llm.client import get_llm
    from app.core.llm.model_selector import Stage
    from app.prompts import get_prompt

    prompt = get_prompt("sql_verifier")
    system = prompt.render(
        views=", ".join(sorted(allow.names)),
        scoped_views="\n".join(f"- {n}: scope column {c}" for n, c in sorted(allow.scope_columns.items()))
        or "- (none)",
        placeholder=f":{scope.placeholder}",
    )
    user = "<sql>\n" + sql.strip() + "\n</sql>"
    resp = await get_llm().complete(
        Stage.guard,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_model=GuardVerdict,
        max_tokens=3000,
        prompt_version_id=prompt.version_id,
    )
    verdict = resp.parsed
    assert isinstance(verdict, GuardVerdict)
    return verdict, prompt.version_id


# ── orchestration ────────────────────────────────────────────────────────────


class SqlGuard:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _hard_rules(self, sql: str, allow: AllowList) -> exp.Expression:
        root = parse_single(sql, allow.dialect if allow.dialect != "postgresql" else "postgres")
        check_read_only(root, sql)
        check_allow_list(root, allow)
        return root

    async def check(
        self, sql: str, *, allow: AllowList, scope: ScopePredicate, mode: Literal["chat", "refresh"]
    ) -> GuardResult:
        dialect = "postgres"
        rules_failed: list[str] = []
        # ── hard deterministic rules (1, 2, 3) ───────────────────────────────
        try:
            root = self._hard_rules(sql, allow)
        except GuardBlockedError as exc:
            metrics.guard_blocks.labels("sql_guard", exc.rule).inc()
            return GuardResult(verdict="blocked", reason=exc.reason, rules_failed=[exc.rule])

        if not scope.has_access:
            metrics.guard_blocks.labels("scope", "no_access").inc()
            return GuardResult(
                verdict="blocked", kind="scope", reason="You have no data access on this source", rules_failed=["scope"]
            )

        # ── parser's structural verdict on rule 5 (shadow in chat mode, enforcer on refresh) ──
        placement = find_scoped_references(root, allow.scope_columns)
        parser_verdict = "allowed"
        if placement.violations:
            uncovered = ", ".join(sorted({f"{r.qualifier} ({r.table.name})" for r in placement.violations}))
            parser_verdict = f"repaired: scope predicate missing on {uncovered}"

        # ── enforcer for rule 5 ─────────────────────────────────────────────
        llm_used = False
        verdict: Verdict = "allowed"
        reason = "compliant"
        working_root = root
        use_llm = mode == "chat" and self.settings.guard_enforcer == "llm" and bool(allow.scope_columns)
        if use_llm:
            llm_used = True
            v, _pv = await llm_verify(sql, allow, scope)
            if v.compliant:
                if placement.violations:
                    metrics.guard_shadow_disagreements.labels("parser_stricter").inc()
            elif v.repaired_sql:
                try:
                    working_root = self._hard_rules(v.repaired_sql, allow)
                except GuardBlockedError as exc:
                    metrics.guard_blocks.labels("scope", "llm_repair_invalid").inc()
                    return GuardResult(
                        verdict="blocked",
                        kind="scope",
                        reason=f"verifier repair rejected: {exc.reason}",
                        rules_failed=["scope", exc.rule],
                        shadow_parser_verdict=parser_verdict,
                        llm_used=True,
                    )
                verdict, reason = "repaired", f"verifier: {v.reason}"
                if not placement.violations:
                    metrics.guard_shadow_disagreements.labels("llm_stricter").inc()
            else:
                metrics.guard_blocks.labels("scope", "llm_verifier").inc()
                if not placement.violations:
                    metrics.guard_shadow_disagreements.labels("llm_stricter").inc()
                return GuardResult(
                    verdict="blocked",
                    kind="scope",
                    reason=f"verifier: {v.reason}",
                    rules_failed=["scope"],
                    shadow_parser_verdict=parser_verdict,
                    llm_used=True,
                )
        elif placement.violations:
            verdict, reason = "repaired", parser_verdict

        # ── deterministic repair + bind: the last line, regardless of who enforced ──
        try:
            working_root, injected = repair_scope(working_root, allow.scope_columns)
        except ValueError as exc:
            metrics.guard_blocks.labels("scope", "unrepairable").inc()
            return GuardResult(
                verdict="blocked",
                kind="scope",
                reason=str(exc),
                rules_failed=["scope"],
                shadow_parser_verdict=parser_verdict,
                llm_used=llm_used,
            )
        if injected and verdict == "allowed":
            verdict, reason = "repaired", f"scope predicate injected on {injected} reference(s)"
        final_check = find_scoped_references(working_root, allow.scope_columns)
        if not final_check.ok:  # cannot happen after repair, but assert the invariant rather than trust it
            metrics.guard_blocks.labels("scope", "unverified").inc()
            return GuardResult(
                verdict="blocked", kind="scope", reason="scope predicate could not be verified", rules_failed=["scope"]
            )
        canonical = working_root.sql(dialect=dialect)
        capped_root, cap_applied = apply_row_cap(working_root.copy(), self.settings.sql_max_rows)
        try:
            bound = bind_scope(capped_root, scope, dialect)
        except ValueError as exc:
            metrics.guard_blocks.labels("scope", "bind_failed").inc()
            return GuardResult(verdict="blocked", kind="scope", reason=str(exc), rules_failed=["scope"])
        if cap_applied:
            rules_failed.append("row_cap")
        return GuardResult(
            verdict=verdict,
            reason=reason,
            kind="scope" if verdict == "repaired" else "sql_guard",
            sql_canonical=canonical,
            sql_bound=bound,
            sql_hash=sql_hash(canonical),
            rules_failed=rules_failed,
            shadow_parser_verdict=parser_verdict,
            llm_used=llm_used,
            scope_injected=injected,
            row_cap_applied=cap_applied,
        )
