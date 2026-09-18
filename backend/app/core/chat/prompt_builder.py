"""Assembles the agent's messages.

Prompt separation: the system prompt is a versioned file; the question, schema,
business rules, scope instruction and prior turns are injected as clearly
delimited *data* blocks.  No user-derived string ever becomes an instruction.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.security.access_scope import ScopePredicate, scope_prompt_instruction
from app.core.security.prompt_injection import wrap_data
from app.core.sql.schema_context import AllowList, render_business_rules, render_schema_context
from app.prompts import get_prompt

MAX_HISTORY_TURNS = 6
MAX_HISTORY_CHARS = 1200


@dataclass(frozen=True, slots=True)
class PriorTurn:
    question: str
    findings: str


def build_messages(
    *, question: str, allow: AllowList, scope: ScopePredicate, history: list[PriorTurn]
) -> tuple[list[dict], str]:
    prompt = get_prompt("agent_system")
    context = "\n\n".join(
        [
            wrap_data("schema", render_schema_context(allow)),
            wrap_data("rules", render_business_rules(allow)),
            wrap_data("scope", scope_prompt_instruction(scope, allow.scoped_view_names)),
            wrap_data(
                "chart_contract",
                "Each run_python call that shows a chart must be self-contained: it starts from ONE DataFrame produced by "
                "run_sql (combine sources in SQL with JOINs/CTEs), builds the figure, sets a title and calls fig.show(). "
                "The same code will be re-executed later against fresh data with no model present.",
            ),
        ]
    )
    messages: list[dict] = [{"role": "system", "content": prompt.text + "\n\n" + context}]
    for prior in history[-MAX_HISTORY_TURNS:]:
        messages.append({"role": "user", "content": wrap_data("question", prior.question[:MAX_HISTORY_CHARS])})
        messages.append({"role": "assistant", "content": prior.findings[:MAX_HISTORY_CHARS] or "(no findings)"})
    messages.append({"role": "user", "content": wrap_data("question", question)})
    return messages, prompt.version_id
