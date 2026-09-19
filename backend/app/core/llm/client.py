"""Native ``openai`` SDK client with timeouts, retries, circuit breaker, fallback chain and accounting.

There is exactly one place in Lens that talks to a model, and this is it.  Every
call:

* runs under a per-call timeout;
* is retried a bounded number of times with jittered exponential backoff;
* is refused while the model's circuit is open;
* falls through an ordered model chain when the primary is unreachable;
* is counted on the request's :class:`LlmCallCounter` (the zero-LLM refresh
  invariant is asserted against that counter);
* records model, prompt version, tokens and cost on the active span.

Structured outputs use the SDK's ``parse`` API with a Pydantic v2 model; tool
calls use strict function schemas generated from Pydantic models.  Nothing is
scraped out of free text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel

from app.api.errors import ProviderUnavailable
from app.config import Settings
from app.core.llm.budgets import Usage, price
from app.core.llm.model_selector import Stage, models_for, temperature_for
from app.core.llm.provider_health import ProviderHealth
from app.observability import langsmith_tracing, metrics
from app.observability.request_context import get_llm_counter
from app.observability.tracing import stage_span

log = logging.getLogger("lens.generation")

T = TypeVar("T", bound=BaseModel)


class Transport(Protocol):
    """The one seam between Lens and the provider SDK (a scripted fake implements it in tests)."""

    async def parse(self, **kwargs: Any) -> ChatCompletion: ...

    async def probe(self) -> bool: ...


class OpenAITransport:
    def __init__(self, settings: Settings) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key.get_secret_value() or "unset",
            base_url=settings.llm_base_url or None,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # retries are ours, so they are observable
        )
        # gateway-specific knobs travel as extra body
        self._extra_body: dict[str, Any] = json.loads(settings.llm_extra_body_json or "{}")

    async def parse(self, **kwargs: Any) -> ChatCompletion:
        if self._extra_body:
            kwargs["extra_body"] = self._extra_body
        return await self._client.beta.chat.completions.parse(**kwargs)

    async def probe(self) -> bool:
        try:
            await self._client.with_options(timeout=5).models.list()
            return True
        except Exception:
            return False


@dataclass(slots=True)
class LlmResponse:
    model: str
    message: Any
    usage: Usage
    parsed: BaseModel | None = None
    tool_calls: list[Any] = field(default_factory=list)
    attempts: int = 1
    finish_reason: str | None = None

    @property
    def text(self) -> str:
        return (self.message.content or "") if self.message is not None else ""


class LlmClient:
    def __init__(self, settings: Settings, transport: Transport | None = None) -> None:
        self.settings = settings
        self.transport: Transport = transport or OpenAITransport(settings)
        self.health = ProviderHealth(settings.llm_circuit_failure_threshold, settings.llm_circuit_cooldown_seconds)
        self.total_calls = 0  # process-wide, for tests

    # ── public API ───────────────────────────────────────────────────────────
    async def complete(
        self,
        stage: Stage,
        messages: list[ChatCompletionMessageParam],
        *,
        response_model: type[T] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        max_tokens: int | None = None,
        prompt_version_id: str | None = None,
        temperature: float | None = None,
    ) -> LlmResponse:
        if not self.settings.llm_configured and not isinstance(self.transport, _FakeMarker):
            raise ProviderUnavailable("No LLM API key is configured; dashboards still work, questions do not.")
        chain = models_for(self.settings, stage)
        temp = temperature_for(self.settings, stage) if temperature is None else temperature
        last_error: Exception | None = None
        for idx, model in enumerate(chain):
            if not self.health.allow(model):
                log.warning("circuit open, skipping model", extra={"model": model, "stage": stage.value})
                last_error = ProviderUnavailable(f"model {model} circuit open")
                continue
            if idx > 0:
                metrics.llm_fallbacks.labels(chain[idx - 1], model).inc()
            try:
                return await self._call_with_retries(
                    stage, model, messages, response_model, tools, tool_choice, max_tokens, temp, prompt_version_id
                )
            except _RetryableFailure as exc:
                last_error = exc.cause
                self.health.failure(model, type(exc.cause).__name__)
                continue
        log.error("all models failed", extra={"stage": stage.value, "chain": chain, "error": type(last_error).__name__})
        raise ProviderUnavailable("We couldn't answer that right now; the model provider is unreachable.")

    async def probe(self) -> bool:
        return await self.transport.probe()

    # ── internals ────────────────────────────────────────────────────────────
    async def _call_with_retries(
        self,
        stage: Stage,
        model: str,
        messages: list[ChatCompletionMessageParam],
        response_model: type[T] | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
        max_tokens: int | None,
        temperature: float,
        prompt_version_id: str | None,
    ) -> LlmResponse:
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if response_model is not None:
            kwargs["response_format"] = response_model
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
            kwargs["parallel_tool_calls"] = False
        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        attempts = self.settings.llm_max_retries + 1
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            with (
                stage_span(
                    f"llm.{stage.value}", model=model, attempt=attempt, prompt_version_id=prompt_version_id
                ) as span,
                langsmith_tracing.llm_run(
                    f"llm.{stage.value}",
                    inputs={"messages": messages},
                    metadata={"model": model, "attempt": attempt, "prompt_version_id": prompt_version_id},
                ) as ls_run,
            ):
                try:
                    completion = await asyncio.wait_for(
                        self.transport.parse(**kwargs), timeout=self.settings.llm_timeout_seconds + 5
                    )
                except (
                    openai.APIConnectionError,
                    openai.APITimeoutError,
                    openai.RateLimitError,
                    openai.InternalServerError,
                    openai.LengthFinishReasonError,
                    openai.ContentFilterFinishReasonError,
                    TimeoutError,
                ) as exc:
                    last = exc
                    metrics.llm_calls.labels(stage.value, model, "error").inc()
                    span.set_attribute("error", type(exc).__name__)
                    ls_run.set_attribute("error", type(exc).__name__)
                    if attempt < attempts:
                        metrics.llm_retries.labels(model).inc()
                        delay = min(8.0, (0.5 * 2 ** (attempt - 1))) * (0.5 + random.random())
                        # a 429 says how long the window is; sleeping less than that just burns the retry
                        delay = max(delay, _retry_after_seconds(exc))
                        log.warning(
                            "llm call failed, retrying",
                            extra={
                                "model": model,
                                "attempt": attempt,
                                "delay_s": round(delay, 2),
                                "error": type(exc).__name__,
                            },
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise _RetryableFailure(exc) from exc
                except openai.APIStatusError as exc:
                    if exc.status_code in (402, 403, 404, 502, 503, 504):
                        # account/credit/availability problems on the provider side: fall through the model chain
                        metrics.llm_calls.labels(stage.value, model, "error").inc()
                        log.warning(
                            "llm provider refused model",
                            extra={"model": model, "status": exc.status_code, "error": type(exc).__name__},
                        )
                        raise _RetryableFailure(exc) from exc
                    # any other 4xx: our request is wrong; don't retry, don't trip the breaker
                    metrics.llm_calls.labels(stage.value, model, "invalid").inc()
                    log.error(
                        "llm rejected request",
                        extra={"model": model, "status": exc.status_code, "error": type(exc).__name__},
                    )
                    raise ProviderUnavailable("The model provider rejected the request.") from None
                except ProviderUnavailable:
                    raise
                except (
                    Exception
                ) as exc:  # a gateway returned a body the SDK could not parse: treat as an outage of that model
                    metrics.llm_calls.labels(stage.value, model, "error").inc()
                    log.warning(
                        "llm transport failure",
                        extra={"model": model, "error": type(exc).__name__, "detail": str(exc)[:200]},
                    )
                    raise _RetryableFailure(exc) from exc
                elapsed = time.perf_counter() - started
                self.health.success(model)
                choice = completion.choices[0]
                usage_raw = completion.usage
                in_tok = int(getattr(usage_raw, "prompt_tokens", 0) or 0)
                out_tok = int(getattr(usage_raw, "completion_tokens", 0) or 0)
                cost = price(self.settings, model, in_tok, out_tok)
                usage = Usage(in_tok, out_tok, cost)
                counter = get_llm_counter()
                counter.calls += 1
                counter.input_tokens += in_tok
                counter.output_tokens += out_tok
                counter.cost_usd = round(counter.cost_usd + cost, 8)
                counter.models.append(model)
                self.total_calls += 1
                metrics.llm_calls.labels(stage.value, model, "ok").inc()
                metrics.stage_latency.labels(f"llm.{stage.value}").observe(elapsed)
                span.set_attribute("input_tokens", in_tok)
                span.set_attribute("output_tokens", out_tok)
                span.set_attribute("cost_usd", cost)
                span.set_attribute("finish_reason", choice.finish_reason or "")
                span.set_attribute("priced", model in self.settings.pricing)
                ls_run.set_attribute("input_tokens", in_tok)
                ls_run.set_attribute("output_tokens", out_tok)
                ls_run.set_attribute("cost_usd", cost)
                ls_run.set_attribute("finish_reason", choice.finish_reason or "")
                ls_run.set_attribute("output_text", choice.message.content or "")
                log.info(
                    "llm call",
                    extra={
                        "stage": stage.value,
                        "model": model,
                        "attempt": attempt,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "cost_usd": cost,
                        "duration_ms": int(elapsed * 1000),
                        "prompt_version_id": prompt_version_id,
                        "finish_reason": choice.finish_reason,
                    },
                )
                msg = choice.message
                parsed = getattr(msg, "parsed", None)
                if response_model is not None and getattr(msg, "refusal", None):
                    raise ProviderUnavailable("The model declined to produce a structured answer.")
                return LlmResponse(
                    model=model,
                    message=msg,
                    usage=usage,
                    parsed=parsed,
                    tool_calls=list(msg.tool_calls or []),
                    attempts=attempt,
                    finish_reason=choice.finish_reason,
                )
        raise _RetryableFailure(last or RuntimeError("no attempts"))


_RETRY_AFTER_TEXT = re.compile(r"try again in (\d+(?:\.\d+)?)\s*(ms|s)\b", re.IGNORECASE)


def _retry_after_seconds(exc: BaseException, cap: float = 15.0) -> float:
    """Provider-suggested wait for a rate limit: the retry-after headers, else the number in the message."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        if headers.get("retry-after-ms"):
            return min(cap, float(headers["retry-after-ms"]) / 1000)
        if headers.get("retry-after"):
            return min(cap, float(headers["retry-after"]))
    except (TypeError, ValueError):
        pass
    m = _RETRY_AFTER_TEXT.search(str(exc))
    if m:
        value = float(m.group(1))
        return min(cap, value / 1000 if m.group(2).lower() == "ms" else value)
    return 0.0


class _RetryableFailure(Exception):
    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause


class _FakeMarker:
    """Mixin marker for scripted transports used in tests (bypasses the api-key check)."""


_client: LlmClient | None = None


def init_llm(settings: Settings, transport: Transport | None = None) -> LlmClient:
    global _client
    _client = LlmClient(settings, transport)
    return _client


def get_llm() -> LlmClient:
    assert _client is not None, "llm client not initialised"
    return _client


def install_transport(transport: Transport) -> None:
    """Test hook: swap the provider transport while keeping breaker/accounting logic real."""
    assert _client is not None
    _client.transport = transport
