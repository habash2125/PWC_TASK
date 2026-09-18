"""Per-model circuit breaker and a cached provider reachability probe."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.observability import metrics


class CircuitState(StrEnum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


@dataclass
class Circuit:
    failure_threshold: int
    cooldown_seconds: float
    failures: int = 0
    opened_at: float | None = None
    state: CircuitState = CircuitState.closed
    last_error: str | None = None

    def allow(self) -> bool:
        if self.state is CircuitState.closed:
            return True
        assert self.opened_at is not None
        if time.monotonic() - self.opened_at >= self.cooldown_seconds:
            self.state = CircuitState.half_open
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.state = CircuitState.closed
        self.last_error = None

    def record_failure(self, error: str) -> None:
        self.failures += 1
        self.last_error = error
        if self.state is CircuitState.half_open or self.failures >= self.failure_threshold:
            self.state = CircuitState.open
            self.opened_at = time.monotonic()


@dataclass
class ProviderHealth:
    failure_threshold: int
    cooldown_seconds: float
    circuits: dict[str, Circuit] = field(default_factory=dict)

    def circuit(self, model: str) -> Circuit:
        if model not in self.circuits:
            self.circuits[model] = Circuit(self.failure_threshold, self.cooldown_seconds)
        return self.circuits[model]

    def allow(self, model: str) -> bool:
        ok = self.circuit(model).allow()
        metrics.llm_circuit_open.labels(model).set(0 if ok else 1)
        return ok

    def success(self, model: str) -> None:
        self.circuit(model).record_success()
        metrics.llm_circuit_open.labels(model).set(0)

    def failure(self, model: str, error: str) -> None:
        c = self.circuit(model)
        c.record_failure(error)
        metrics.llm_circuit_open.labels(model).set(1 if c.state is CircuitState.open else 0)

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {
            m: {"state": c.state.value, "failures": c.failures, "last_error": c.last_error}
            for m, c in self.circuits.items()
        }
