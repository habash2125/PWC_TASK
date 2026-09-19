"""The turn lifecycle (Section 7).

 1  AuthN/AuthZ        — resolved by the route dependencies before we get here
 2  Injection screen   — pre-screen + guard model, structured verdict
 3  Context assembly   — allow-list schema + rules ∥ row-level scope (fail-closed)
 4  Code generation    — native tool-calling loop, temperature 0, bounded
 5  SQL guard          — inside the loop, before every execution
 6  Execution          — read-only pool, statement timeout, errors fed back
 7  Chart capture      — patched fig.show(), Pydantic envelope
 8  Narrative          — small model, chart titles only
 9  Placement          — anchors resolved positionally, nothing dropped
10  Persist + trace    — turn, turn_chart, guard_event, usage, spans
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import BudgetExceeded, ProviderUnavailable, ScopeUnavailable
from app.config import Settings
from app.core.auth.rbac import Principal
from app.core.chat.answer_phrasing import phrase_answer
from app.core.chat.chart_placement import place_charts
from app.core.chat.prompt_builder import PriorTurn, build_messages
from app.core.llm.budgets import Usage, check_daily_ceiling
from app.core.runtime.agent_loop import AgentLoop, AgentOutcome
from app.core.security.access_scope import DEFAULT_SCOPE_KEY, DbScopeSource, ScopePredicate
from app.core.security.prompt_injection import screen_question
from app.core.sql.schema_context import AllowList, load_allow_list
from app.core.sql.sql_guard import SqlGuard
from app.db.models import ChatSession, GuardKind, GuardVerdict, Turn, TurnStatus
from app.db.repos.audit import AuditRepo
from app.db.repos.chat import ChatRepo
from app.db.session import session_factory
from app.observability import metrics
from app.observability.request_context import get_llm_counter, session_id_var, trace_id_var, turn_id_var
from app.observability.tracing import stage_span

log = logging.getLogger("lens.generation")


@dataclass(slots=True)
class TurnResult:
    turn: Turn
    charts: list[dict[str, Any]] = field(default_factory=list)
    chart_order: list[int] = field(default_factory=list)
    guard_events: list[dict[str, Any]] = field(default_factory=list)


async def run_turn(
    session: AsyncSession, settings: Settings, principal: Principal, chat: ChatSession, message: str, *, meta: dict
) -> TurnResult:
    repo = ChatRepo(session)
    audit = AuditRepo(session)
    started = time.perf_counter()
    trace_id = trace_id_var.get() or uuid.uuid4().hex
    session_id_var.set(str(chat.id))
    timings: dict[str, int] = {}
    usage = Usage()
    counter = get_llm_counter()

    # ── budget ceilings (fail loudly) ─────────────────────────────────────────
    today = await repo.usage_today(principal.id)
    check_daily_ceiling(
        settings,
        turn_count=today.turn_count if today else 0,
        tokens=(today.input_tokens + today.output_tokens) if today else 0,
        cost_usd=float(today.cost_usd) if today else 0.0,
    )

    async def _persist(
        status: TurnStatus,
        *,
        answer: str | None,
        error_code: str | None,
        outcome: AgentOutcome | None,
        guard_events: list[dict[str, Any]],
        prompt_version: str | None,
        model: str | None,
    ) -> TurnResult:
        total_ms = int((time.perf_counter() - started) * 1000)
        turn = await repo.create_turn(
            session_id=chat.id,
            user_id=principal.id,
            question=message,
            answer_markdown=answer,
            status=status,
            error_code=error_code,
            sql_text=(outcome.datasets[-1].sql_canonical if outcome and outcome.datasets else None),
            prompt_version_id=prompt_version,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            duration_ms=total_ms,
            stage_timings={**timings, "total": total_ms},
            trace_id=trace_id,
        )
        turn_id_var.set(str(turn.id))
        result = TurnResult(turn=turn)
        if outcome:
            for i, chart in enumerate(outcome.charts):
                tc = await repo.add_turn_chart(
                    turn,
                    position=i,
                    title=chart.title,
                    chart_spec=chart.spec,
                    sql_text=chart.sql_text,
                    sql_hash=chart.sql_hash,
                    render_code=chart.render_code,
                    dataset_name=chart.dataset_name,
                )
                result.charts.append(
                    {
                        "id": tc.id,
                        "position": i,
                        "title": tc.title,
                        "chart_spec": tc.chart_spec,
                        "sql_text": tc.sql_text,
                        "sql_hash": tc.sql_hash,
                    }
                )
        for ev in guard_events:
            await repo.add_guard_event(
                turn_id=turn.id,
                tile_refresh_id=None,
                kind=GuardKind(ev["kind"]),
                verdict=GuardVerdict(ev["verdict"]),
                reason=ev["reason"],
                offending_sql=ev.get("offending_sql"),
                shadow=ev.get("shadow_parser_verdict"),
            )
            if ev["verdict"] == "blocked":
                await audit.write(
                    action="guard.block",
                    tenant_id=principal.tenant_id,
                    actor_user_id=principal.id,
                    object_type="turn",
                    object_id=turn.id,
                    metadata={"kind": ev["kind"], "reason": ev["reason"][:300]},
                    **meta,
                )
        result.guard_events = guard_events
        await repo.add_usage(
            principal.id,
            turns=1,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
        )
        await repo.touch_session(chat, title=message[:80])
        metrics.turn_status.labels(status.value).inc()
        metrics.turn_tokens.labels("input").observe(usage.input_tokens)
        metrics.turn_tokens.labels("output").observe(usage.output_tokens)
        metrics.turn_cost_usd.observe(usage.cost_usd)
        for stage, ms in timings.items():
            metrics.stage_latency.labels(stage).observe(ms / 1000)
        log.info(
            "turn finished",
            extra={
                "status": status.value,
                "error_code": error_code,
                "duration_ms": total_ms,
                "charts": len(result.charts),
                "llm_calls": counter.calls,
                "cost_usd": usage.cost_usd,
                "model": model,
            },
        )
        return result

    # ── 2. injection screen ───────────────────────────────────────────────────
    t0 = time.perf_counter()
    with stage_span("screen") as span:
        try:
            screen = await screen_question(message)
        except ProviderUnavailable as exc:
            timings["screen"] = int((time.perf_counter() - t0) * 1000)
            usage = _usage_from_counter(counter)
            return await _persist(
                TurnStatus.error,
                answer=exc.detail,
                error_code="provider_unavailable",
                outcome=None,
                guard_events=[],
                prompt_version=None,
                model=None,
            )
        span.set_attribute("allowed", screen.allowed)
        span.set_attribute("category", screen.category)
        span.set_attribute("stage", screen.stage)
    timings["screen"] = int((time.perf_counter() - t0) * 1000)
    if not screen.allowed:
        usage = _usage_from_counter(counter)
        ev = {
            "kind": "injection",
            "verdict": "blocked",
            "reason": f"{screen.category}: {screen.reason}",
            "offending_sql": None,
        }
        return await _persist(
            TurnStatus.blocked,
            answer="I can't help with that request. Ask a question about the delivery portfolio data.",
            error_code=f"injection:{screen.category}",
            outcome=None,
            guard_events=[ev],
            prompt_version=None,
            model=None,
        )
    question = screen.question

    # ── 3. context assembly: schema ∥ scope, fail-closed ─────────────────────
    t0 = time.perf_counter()
    with stage_span("context") as span:
        try:
            allow, scope = await asyncio.gather(
                load_allow_list(session, chat.data_source_id),
                DbScopeSource(session_factory()).fetch(principal.id, chat.data_source_id, DEFAULT_SCOPE_KEY),
            )
        except ScopeUnavailable as exc:
            timings["context"] = int((time.perf_counter() - t0) * 1000)
            ev = {"kind": "scope", "verdict": "blocked", "reason": "scope source unavailable", "offending_sql": None}
            return await _persist(
                TurnStatus.blocked,
                answer=exc.detail,
                error_code="scope_unavailable",
                outcome=None,
                guard_events=[ev],
                prompt_version=None,
                model=None,
            )
        span.set_attribute("views", len(allow.views))
        span.set_attribute("scope", scope.describe())
    timings["context"] = int((time.perf_counter() - t0) * 1000)
    if not scope.has_access:
        ev = {"kind": "scope", "verdict": "blocked", "reason": "no access scope assigned", "offending_sql": None}
        return await _persist(
            TurnStatus.blocked,
            answer="You don't have data access on this source; ask an administrator for a scope.",
            error_code="no_scope",
            outcome=None,
            guard_events=[ev],
            prompt_version=None,
            model=None,
        )

    history = [PriorTurn(t.question, t.answer_markdown or "") for t in await repo.recent_turns(chat.id)]
    messages, prompt_version = build_messages(question=question, allow=allow, scope=scope, history=history)

    # ── 4–7. agent loop ───────────────────────────────────────────────────────
    with stage_span("agent", prompt_version_id=prompt_version) as span:
        outcome = await AgentLoop(settings, SqlGuard(settings)).run(
            messages=messages, allow=allow, scope=scope, prompt_version_id=prompt_version
        )
        span.set_attribute("status", outcome.status)
        span.set_attribute("steps", outcome.steps)
        span.set_attribute("charts", len(outcome.charts))
        span.set_attribute("model", outcome.model or "")
    timings.update(outcome.stage_timings)
    usage = usage + outcome.usage
    guard_events = [
        {
            "kind": e.kind,
            "verdict": e.verdict,
            "reason": e.reason,
            "offending_sql": e.offending_sql,
            "shadow_parser_verdict": e.shadow_parser_verdict,
        }
        for e in outcome.guard_events
    ]
    if outcome.status != "ok":
        status = {"blocked": TurnStatus.blocked, "timeout": TurnStatus.timeout}.get(outcome.status, TurnStatus.error)
        answer = outcome.error_message or "The question could not be answered."
        if outcome.status == "blocked":
            answer = "That request was blocked by a safety guard: " + (
                outcome.guard_events[-1].reason if outcome.guard_events else answer
            )
        return await _persist(
            status,
            answer=answer,
            error_code=outcome.error_code,
            outcome=outcome,
            guard_events=guard_events,
            prompt_version=prompt_version,
            model=outcome.model,
        )

    # ── 8–9. narrative + placement ────────────────────────────────────────────
    t0 = time.perf_counter()
    with stage_span("narrative") as span:
        markdown, n_usage, _ = await phrase_answer(
            question=question, findings=outcome.findings, chart_titles=[c.title for c in outcome.charts]
        )
        usage = usage + n_usage
        span.set_attribute("chars", len(markdown))
    timings["narrative"] = int((time.perf_counter() - t0) * 1000)
    markdown, order = place_charts(markdown, len(outcome.charts))

    result = await _persist(
        TurnStatus.ok,
        answer=markdown,
        error_code=None,
        outcome=outcome,
        guard_events=guard_events,
        prompt_version=prompt_version,
        model=outcome.model,
    )
    result.chart_order = order
    return result


def _usage_from_counter(counter) -> Usage:
    return Usage(counter.input_tokens, counter.output_tokens, counter.cost_usd)


__all__ = ["AllowList", "ScopePredicate", "TurnResult", "run_turn", "BudgetExceeded"]
