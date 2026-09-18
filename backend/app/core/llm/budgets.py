"""Token and cost accounting with per-turn and per-user-per-day ceilings that fail loudly."""

from __future__ import annotations

from dataclasses import dataclass

from app.api.errors import BudgetExceeded
from app.config import Settings


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            round(self.cost_usd + other.cost_usd, 8),
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def price(settings: Settings, model: str, input_tokens: int, output_tokens: int) -> float:
    table = settings.pricing
    if model not in table:
        # unknown model: cost recorded as zero but flagged in the span attributes by the caller
        return 0.0
    in_rate, out_rate = table[model]
    return round((input_tokens * in_rate + output_tokens * out_rate) / 1_000_000, 8)


@dataclass(slots=True)
class TurnBudget:
    """Enforced inside the agent loop after every model call."""

    max_tokens: int
    used: Usage = Usage()

    def charge(self, usage: Usage) -> None:
        self.used = self.used + usage
        if self.used.total_tokens > self.max_tokens:
            raise BudgetExceeded(
                f"This question used more than the per-turn token ceiling ({self.max_tokens} tokens); try a narrower question."
            )


def check_daily_ceiling(settings: Settings, *, turn_count: int, tokens: int, cost_usd: float) -> None:
    """Called before a turn starts with today's totals for the user."""
    if turn_count >= settings.daily_turn_ceiling:
        raise BudgetExceeded(f"Daily question ceiling reached ({settings.daily_turn_ceiling} questions).")
    if tokens >= settings.daily_token_ceiling:
        raise BudgetExceeded("Daily token ceiling reached.")
    if cost_usd >= settings.daily_cost_ceiling_usd:
        raise BudgetExceeded("Daily cost ceiling reached.")
