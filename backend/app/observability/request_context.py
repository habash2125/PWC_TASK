"""Per-request correlation identifiers carried in ContextVars.

Every log line, span and error response reads from here, so one identifier
ties a user complaint to the exact model call and the exact SQL statement.
"""

from __future__ import annotations

import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)
turn_id_var: ContextVar[str | None] = ContextVar("turn_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)


@dataclass
class LlmCallCounter:
    """Counts model calls made while a request/turn is in flight.

    The zero-LLM invariant for dashboard refresh is asserted against this.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    models: list[str] = field(default_factory=list)


llm_counter_var: ContextVar[LlmCallCounter | None] = ContextVar("llm_counter", default=None)


def new_id(nbytes: int = 8) -> str:
    return secrets.token_hex(nbytes)


def current_context() -> dict[str, Any]:
    return {
        "request_id": request_id_var.get(),
        "trace_id": trace_id_var.get(),
        "user_id": user_id_var.get(),
        "session_id": session_id_var.get(),
        "turn_id": turn_id_var.get(),
        "tenant_id": tenant_id_var.get(),
    }


def get_llm_counter() -> LlmCallCounter:
    counter = llm_counter_var.get()
    if counter is None:
        counter = LlmCallCounter()
        llm_counter_var.set(counter)
    return counter
