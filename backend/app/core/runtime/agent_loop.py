"""The native tool-calling agent loop (~200 lines on the ``openai`` SDK).

Two tools, bounded steps, stdout fed back.  Every ``run_sql`` goes through the
guard before the read-only pool; every ``run_python`` runs in the sandbox.  A
SQL or Python error becomes a tool result the model can correct, not a failed
turn.  A loop guard aborts on repeated identical executions; a wall-clock
budget and a token budget bound the whole thing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
from openai import pydantic_function_tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.errors import BudgetExceeded, ProviderUnavailable
from app.config import Settings
from app.core.llm.budgets import TurnBudget, Usage
from app.core.llm.client import LlmResponse, get_llm
from app.core.llm.model_selector import Stage
from app.core.runtime.chart_capture import CapturedChart
from app.core.runtime.python_exec import run_python
from app.core.security.access_scope import ScopePredicate
from app.core.security.prompt_injection import wrap_data
from app.core.security.redaction import redact_preview
from app.core.sql.schema_context import AllowList
from app.core.sql.sql_guard import GuardResult, SqlGuard
from app.db.analytics_pool import AnalyticsQueryError, execute_readonly
from app.observability import metrics
from app.observability.tracing import stage_span

log = logging.getLogger("lens.generation")


class RunSqlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(
        description="One read-only PostgreSQL SELECT against the allow-listed views, with the scope placeholder"
    )
    name: str = Field(description="Variable name for the resulting DataFrame, e.g. 'df'")


class RunPythonArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(description="Python using the fetched DataFrames; call fig.show() on each chart to deliver it")


TOOLS = [
    pydantic_function_tool(
        RunSqlArgs, name="run_sql", description="Run one read-only SQL SELECT and keep the result as a named DataFrame."
    ),
    pydantic_function_tool(
        RunPythonArgs,
        name="run_python",
        description="Run Python over the fetched DataFrames; deliver charts with fig.show().",
    ),
]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(slots=True)
class Dataset:
    name: str
    sql_canonical: str
    sql_hash: str
    guard: GuardResult
    row_count: int


@dataclass(slots=True)
class AgentChart:
    title: str
    spec: dict[str, Any]
    sql_text: str
    sql_hash: str
    render_code: str
    dataset_name: str
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GuardEventRecord:
    kind: Literal["sql_guard", "scope", "loop", "budget"]
    verdict: Literal["allowed", "repaired", "blocked"]
    reason: str
    offending_sql: str | None = None
    shadow_parser_verdict: str | None = None


@dataclass(slots=True)
class AgentOutcome:
    status: Literal["ok", "blocked", "error", "timeout"]
    findings: str = ""
    charts: list[AgentChart] = field(default_factory=list)
    datasets: list[Dataset] = field(default_factory=list)
    steps: int = 0
    usage: Usage = Usage()
    model: str | None = None
    prompt_version_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    guard_events: list[GuardEventRecord] = field(default_factory=list)
    stage_timings: dict[str, int] = field(default_factory=dict)


def _to_frame(columns: list[str], rows: list[tuple[Any, ...]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=columns)
    for col in df.columns:
        sample = next((v for v in df[col] if v is not None), None)
        if isinstance(sample, decimal.Decimal):
            df[col] = df[col].astype(float)
        elif isinstance(sample, (dt.date, dt.datetime)) and not isinstance(sample, pd.Timestamp):
            df[col] = pd.to_datetime(df[col])
    return df


def _preview(
    df: pd.DataFrame, columns: list[str], rows: list[tuple[Any, ...]], sensitive: set[str], truncated: bool
) -> str:
    dtypes = {c: str(t) for c, t in df.dtypes.items()}
    preview = redact_preview(columns, rows, sensitive, limit=5)
    body = {
        "columns": columns,
        "dtypes": dtypes,
        "row_count": len(rows),
        "truncated": truncated,
        "preview_rows": [[_json_safe(v) for v in r] for r in preview],
    }
    return wrap_data("sql_result", json.dumps(body, default=str))


def _json_safe(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def _referenced_datasets(code: str, datasets: dict[str, Dataset]) -> list[Dataset]:
    names = set(_IDENT.findall(code))
    return [d for d in datasets.values() if d.name in names]


class AgentLoop:
    def __init__(self, settings: Settings, guard: SqlGuard):
        self.settings = settings
        self.guard = guard

    async def run(
        self,
        *,
        messages: list[dict],
        allow: AllowList,
        scope: ScopePredicate,
        prompt_version_id: str,
        guard_mode: Literal["chat", "refresh"] = "chat",
    ) -> AgentOutcome:
        started = time.perf_counter()
        deadline = started + self.settings.turn_timeout_seconds
        outcome = AgentOutcome(status="ok", prompt_version_id=prompt_version_id)
        budget = TurnBudget(max_tokens=self.settings.turn_max_tokens)
        namespace: dict[str, Any] = {}
        datasets: dict[str, Dataset] = {}
        seen_calls: dict[str, int] = {}
        sql_failures = 0
        sensitive = {c for cols in allow.sensitive_columns.values() for c in cols}
        llm = get_llm()
        timings = {"gen": 0, "sql_guard": 0, "sql": 0, "python": 0}
        nudges = 0

        for step in range(1, self.settings.turn_max_steps + 1):
            if time.perf_counter() > deadline:
                outcome.status, outcome.error_code, outcome.error_message = (
                    "timeout",
                    "turn_timeout",
                    "The question took too long to answer",
                )
                break
            outcome.steps = step
            t0 = time.perf_counter()
            try:
                resp: LlmResponse = await asyncio.wait_for(
                    llm.complete(
                        Stage.agent,
                        messages,
                        tools=TOOLS,
                        prompt_version_id=prompt_version_id,
                        max_tokens=4000,
                        tool_choice="required"
                        if not datasets
                        else "auto",  # an answer is never accepted before data was fetched
                    ),
                    timeout=max(1.0, deadline - time.perf_counter()),
                )
            except TimeoutError:
                outcome.status, outcome.error_code, outcome.error_message = (
                    "timeout",
                    "turn_timeout",
                    "The question took too long to answer",
                )
                break
            except ProviderUnavailable as exc:
                outcome.status, outcome.error_code, outcome.error_message = "error", "provider_unavailable", exc.detail
                break
            timings["gen"] += int((time.perf_counter() - t0) * 1000)
            outcome.usage = outcome.usage + resp.usage
            outcome.model = resp.model
            try:
                budget.charge(resp.usage)
            except BudgetExceeded as exc:
                outcome.guard_events.append(GuardEventRecord("budget", "blocked", exc.detail))
                outcome.status, outcome.error_code, outcome.error_message = "blocked", "budget", exc.detail
                break

            messages.append(_assistant_message(resp))
            if not resp.tool_calls:
                # deterministic grounding check: text without a query behind it is not an answer
                if not datasets or (not outcome.charts and nudges == 0):
                    nudges += 1
                    if not datasets and nudges > 2:
                        outcome.status, outcome.error_code = "error", "ungrounded_answer"
                        outcome.error_message = "The model answered without querying the data; the answer was discarded"
                        metrics.guard_blocks.labels("loop", "ungrounded_answer").inc()
                        break
                    if not datasets:
                        messages.append(
                            {
                                "role": "user",
                                "content": "Not acceptable: no data has been queried. Every answer must be grounded in a run_sql result. Call run_sql now.",
                            }
                        )
                        continue
                    if not outcome.charts:
                        messages.append(
                            {
                                "role": "user",
                                "content": f"Deliver at least one chart: call run_python, build a Plotly figure from `{list(datasets)[-1]}` with a title and call fig.show(). Then give the final summary.",
                            }
                        )
                        continue
                outcome.findings = resp.text.strip()
                if resp.finish_reason == "length":
                    outcome.findings += "\n\n(answer truncated)"
                break

            for call in resp.tool_calls:
                name = call.function.name
                raw_args = call.function.arguments or "{}"
                signature = hashlib.sha256(f"{name}:{raw_args}".encode()).hexdigest()
                seen_calls[signature] = seen_calls.get(signature, 0) + 1
                if seen_calls[signature] >= 3:
                    reason = f"the same {name} call was repeated {seen_calls[signature]} times without change"
                    outcome.guard_events.append(GuardEventRecord("loop", "blocked", reason))
                    metrics.guard_blocks.labels("loop", "repeated_call").inc()
                    outcome.status, outcome.error_code, outcome.error_message = "error", "loop_guard", reason
                    return self._finish(outcome, timings, started)

                if name == "run_sql":
                    result_text = await self._tool_run_sql(
                        raw_args, allow, scope, namespace, datasets, sensitive, outcome, timings, guard_mode
                    )
                    if result_text.startswith("BLOCKED") or result_text.startswith("ERROR"):
                        sql_failures += 1
                        if sql_failures > self.settings.turn_max_sql_retries:
                            outcome.status = "blocked" if result_text.startswith("BLOCKED") else "error"
                            outcome.error_code = "sql_retries_exhausted"
                            outcome.error_message = "The generated SQL could not be made to run within the retry budget"
                            return self._finish(outcome, timings, started)
                elif name == "run_python":
                    result_text = await self._tool_run_python(raw_args, namespace, datasets, outcome, timings)
                else:
                    result_text = f"ERROR: unknown tool {name}"
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result_text})
        else:
            outcome.status, outcome.error_code, outcome.error_message = (
                "error",
                "max_steps",
                "The analysis needed more steps than allowed",
            )
        if outcome.status == "ok" and not outcome.findings and not outcome.charts:
            outcome.status, outcome.error_code, outcome.error_message = "error", "no_answer", "No answer was produced"
        return self._finish(outcome, timings, started)

    # ── tools ────────────────────────────────────────────────────────────────

    async def _tool_run_sql(
        self, raw_args, allow, scope, namespace, datasets, sensitive, outcome, timings, guard_mode
    ) -> str:
        try:
            args = RunSqlArgs.model_validate_json(raw_args)
        except ValidationError as exc:
            return f"ERROR: invalid arguments: {exc.errors()[0].get('msg')}"
        name = args.name if _IDENT.fullmatch(args.name) and args.name not in {"pd", "np", "px", "go"} else "df"
        t0 = time.perf_counter()
        with stage_span("sql.guard") as span:
            guard = await self.guard.check(args.sql, allow=allow, scope=scope, mode=guard_mode)
            span.set_attribute("verdict", guard.verdict)
            span.set_attribute("reason", guard.reason[:300])
            span.set_attribute("llm_used", guard.llm_used)
            if guard.shadow_parser_verdict:
                span.set_attribute("shadow_parser_verdict", guard.shadow_parser_verdict[:300])
        timings["sql_guard"] += int((time.perf_counter() - t0) * 1000)
        if guard.verdict != "allowed":
            outcome.guard_events.append(
                GuardEventRecord(guard.kind, guard.verdict, guard.reason, args.sql, guard.shadow_parser_verdict)
            )
        if guard.blocked:
            log.warning("sql blocked", extra={"reason": guard.reason, "rules": guard.rules_failed})
            return f"BLOCKED by the SQL guard: {guard.reason}. Rewrite the query so that it complies (only allow-listed views, single read-only SELECT, scope predicate inside every SELECT that reads a scoped view)."
        assert guard.sql_bound and guard.sql_canonical and guard.sql_hash
        t1 = time.perf_counter()
        with stage_span("sql.execute", sql_hash=guard.sql_hash) as span:
            try:
                result = await execute_readonly(
                    guard.sql_bound,
                    statement_timeout_ms=self.settings.sql_statement_timeout_ms,
                    max_rows=self.settings.sql_max_rows,
                )
            except AnalyticsQueryError as exc:
                span.set_attribute("error", exc.code)
                timings["sql"] += int((time.perf_counter() - t1) * 1000)
                return f"ERROR from the database ({exc.code}): {exc}. Fix the statement and try again."
            span.set_attribute("row_count", result.row_count)
        timings["sql"] += int((time.perf_counter() - t1) * 1000)
        df = _to_frame(result.columns, result.rows)
        namespace[name] = df
        datasets[name] = Dataset(
            name=name,
            sql_canonical=guard.sql_canonical,
            sql_hash=guard.sql_hash,
            guard=guard,
            row_count=result.row_count,
        )
        outcome.datasets = list(datasets.values())
        log.info(
            "sql tool ok",
            extra={
                "dataset": name,
                "row_count": result.row_count,
                "sql_hash": guard.sql_hash,
                "verdict": guard.verdict,
            },
        )
        return f"Stored {result.row_count} rows as `{name}`.\n" + _preview(
            df, result.columns, result.rows, sensitive, result.truncated
        )

    async def _tool_run_python(self, raw_args, namespace, datasets, outcome, timings) -> str:
        try:
            args = RunPythonArgs.model_validate_json(raw_args)
        except ValidationError as exc:
            return f"ERROR: invalid arguments: {exc.errors()[0].get('msg')}"
        t0 = time.perf_counter()
        with stage_span("python.execute") as span:
            result = await run_python(
                args.code,
                namespace,
                timeout_seconds=self.settings.py_exec_timeout_seconds,
                memory_mb=self.settings.py_exec_memory_mb,
                cpu_seconds=self.settings.py_exec_cpu_seconds,
                scratch_root=self.settings.py_exec_scratch_dir,
            )
            span.set_attribute("ok", result.ok)
            span.set_attribute("figures", len(result.figures))
            if result.error_code:
                span.set_attribute("error_code", result.error_code)
        timings["python"] += int((time.perf_counter() - t0) * 1000)
        namespace.update(result.variables)
        referenced = _referenced_datasets(args.code, datasets)
        captured_titles: list[str] = []
        if result.figures and len(referenced) != 1:
            # the artefact contract: one chart ⇐ one query ⇐ one DataFrame, so a refresh can re-run it deterministically
            metrics.chart_capture_failures.labels("no_dataset" if not referenced else "multi_dataset").inc()
            if not referenced:
                return (
                    "ERROR: the chart was built without a run_sql DataFrame; fetch the data with run_sql first and build the "
                    "figure from that DataFrame in the same run_python call."
                )
            return (
                f"ERROR: this code reads {len(referenced)} run_sql DataFrames ({', '.join(d.name for d in referenced)}). A chart must be "
                "built from exactly ONE DataFrame: combine the sources in a single SQL statement (JOIN or CTE), run_sql it, "
                "then build the figure from that one DataFrame."
            )
        dataset = referenced[0] if referenced else None
        for fig in result.figures:
            assert dataset is not None
            outcome.charts.append(
                AgentChart(
                    title=fig.title,
                    spec=fig.spec.model_dump(),
                    sql_text=dataset.sql_canonical,
                    sql_hash=dataset.sql_hash,
                    render_code=args.code,
                    dataset_name=dataset.name,
                    warnings=list(fig.warnings),
                )
            )
            captured_titles.append(fig.title)
        parts = []
        if result.stdout.strip():
            parts.append(wrap_data("stdout", result.stdout.strip()[-6000:]))
        if captured_titles:
            parts.append(f"Delivered {len(captured_titles)} chart(s): " + "; ".join(captured_titles))
        warnings = [w for f in result.figures for w in f.warnings]
        if warnings:
            parts.append("WARNINGS: " + " | ".join(warnings))
        if not result.ok:
            metrics.chart_capture_failures.labels(result.error_code or "exception").inc() if "figure" in (
                result.error or ""
            ) else None
            parts.append(f"ERROR ({result.error_code}): {result.error}")
        if not parts:
            parts.append("(no output)")
        return "\n".join(parts)

    def _finish(self, outcome: AgentOutcome, timings: dict[str, int], started: float) -> AgentOutcome:
        timings["agent_total"] = int((time.perf_counter() - started) * 1000)
        outcome.stage_timings = timings
        return outcome


def _assistant_message(resp: LlmResponse) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": resp.text or None}
    if resp.tool_calls:
        msg["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in resp.tool_calls
        ]
    return msg


__all__ = [
    "AgentChart",
    "AgentLoop",
    "AgentOutcome",
    "CapturedChart",
    "GuardEventRecord",
    "RunPythonArgs",
    "RunSqlArgs",
    "TOOLS",
]
