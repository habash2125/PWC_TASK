"""A scripted provider transport.

Only the model is scripted.  The guard, sandbox, chart capture, placement,
persistence and refresh paths under test are the real ones.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from openai.types.chat.parsed_chat_completion import ParsedChatCompletion, ParsedChatCompletionMessage, ParsedChoice
from openai.types.chat.parsed_function_tool_call import ParsedFunction, ParsedFunctionToolCall
from openai.types.completion_usage import CompletionUsage
from pydantic import BaseModel

from app.core.llm.client import _FakeMarker


def _completion(model: str, message: ParsedChatCompletionMessage, finish_reason: str = "stop") -> ParsedChatCompletion:
    return ParsedChatCompletion(
        id=f"fake-{uuid.uuid4().hex[:8]}",
        object="chat.completion",
        created=0,
        model=model,
        choices=[ParsedChoice(index=0, finish_reason=finish_reason, message=message)],
        usage=CompletionUsage(prompt_tokens=120, completion_tokens=40, total_tokens=160),
    )


class ScriptedTransport(_FakeMarker):
    """``steps`` drive the agent stage; structured stages answer from ``verdicts``."""

    def __init__(
        self,
        steps: list[dict[str, Any]],
        *,
        narrative: str | None = None,
        screen_block: str | None = None,
        guard: dict[str, Any] | None = None,
        grouping: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
    ):
        self.steps = list(steps)
        self.narrative = narrative
        self.screen_block = screen_block
        self.guard = guard or {"compliant": True, "repaired_sql": None, "reason": "ok"}
        self.grouping = grouping
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def probe(self) -> bool:
        return True

    async def parse(self, **kwargs: Any):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        model = kwargs["model"]
        response_format = kwargs.get("response_format")
        if isinstance(response_format, type) and issubclass(response_format, BaseModel):
            name = response_format.__name__
            if name == "ScreenVerdict":
                payload = (
                    {"verdict": "block", "category": self.screen_block, "reason": "scripted block"}
                    if self.screen_block
                    else {"verdict": "allow", "category": "none", "reason": "looks like a data question"}
                )
            elif name == "GuardVerdict":
                payload = self.guard
            elif name == "GroupingProposal":
                payload = self.grouping or {"groups": [], "rationale": ""}
            else:
                raise AssertionError(f"unexpected structured stage {name}")
            parsed = response_format.model_validate(payload)
            msg = ParsedChatCompletionMessage(role="assistant", content=json.dumps(payload), parsed=parsed)
            return _completion(model, msg)
        if kwargs.get("tools"):
            if not self.steps:
                raise AssertionError("script exhausted")
            step = self.steps.pop(0)
            if "tool" in step:
                call = ParsedFunctionToolCall(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    type="function",
                    function=ParsedFunction(
                        name=step["tool"], arguments=json.dumps(step["args"]), parsed_arguments=step["args"]
                    ),
                )
                msg = ParsedChatCompletionMessage(role="assistant", content=None, tool_calls=[call])
                return _completion(model, msg, finish_reason="tool_calls")
            msg = ParsedChatCompletionMessage(role="assistant", content=step["text"])
            return _completion(model, msg)
        # narrative stage: no tools, no response_format
        text = self.narrative if self.narrative is not None else "Here is what the data shows.\n\n<chart 1>\n"
        return _completion(model, ParsedChatCompletionMessage(role="assistant", content=text))


SQL_OVERVIEW = (
    "SELECT project_name, client_name, budget_burn_ratio, milestone_completion_ratio FROM v_delivery_health "
    "WHERE client_id IN (:lens_scope_client_id) AND budget_burn_ratio > 0.8 AND milestone_completion_ratio < 0.5 ORDER BY budget_burn_ratio DESC"
)
CODE_BAR = (
    "fig = px.bar(df, x='project_name', y='budget_burn_ratio', color='client_name', title='Projects over 80% burn with under half of milestones closed')\n"
    "fig.update_layout(yaxis_tickformat='.0%')\n"
    "fig.show()\n"
    "print(df.to_string())"
)


def happy_path_script() -> list[dict[str, Any]]:
    return [
        {"tool": "run_sql", "args": {"sql": SQL_OVERVIEW, "name": "df"}},
        {"tool": "run_python", "args": {"code": CODE_BAR}},
        {
            "text": "Four active projects have burned more than 80% of budget with under half their milestones closed; Network Analytics Migration is the worst at 141%."
        },
    ]
