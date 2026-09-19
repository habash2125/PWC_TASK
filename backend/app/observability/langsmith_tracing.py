"""Optional LangSmith run logging for every model call, alongside the OTel tracer.

LangSmith is a third-party SaaS; unlike ``app.observability.tracing`` (which owns
the in-app trace viewer), this is a second, external record of the same calls,
useful for LangSmith's prompt-diffing and eval UI. It is entirely optional: with
no API key configured, ``llm_run`` is a no-op. Same rule as the OTel tracer:
observability must never break or slow down a request, so every call here is
best-effort and swallows its own failures.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

log = logging.getLogger("lens.generation")

_client: Any | None = None
_project = "lens"


def setup_langsmith(settings: Settings) -> None:
    global _client, _project
    if not settings.langsmith_enabled:
        return
    try:
        from langsmith import Client

        _client = Client(
            api_key=settings.langsmith_api_key.get_secret_value(),
            api_url=settings.langsmith_endpoint or None,
        )
        _project = settings.langsmith_project
    except Exception:
        log.warning("langsmith setup failed; continuing without it", exc_info=True)
        _client = None


def shutdown_langsmith() -> None:
    global _client
    if _client is not None:
        try:
            _client.flush()
        except Exception:
            pass
    _client = None


class _Run:
    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        self.attrs: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        try:
            self.attrs[key] = value
        except Exception:
            pass


class _NoopRun:
    def set_attribute(self, key: str, value: Any) -> None:
        pass


@contextmanager
def llm_run(name: str, *, inputs: dict[str, Any], metadata: dict[str, Any]):
    """Logs one model call as a LangSmith run; a no-op unless a key is configured."""
    if _client is None:
        yield _NoopRun()
        return
    run_id = uuid.uuid4()
    try:
        _client.create_run(
            id=run_id,
            name=name,
            run_type="llm",
            inputs=inputs,
            extra={"metadata": metadata},
            project_name=_project,
            start_time=datetime.now(UTC),
        )
        run: _Run | _NoopRun = _Run(run_id)
    except Exception:
        log.debug("langsmith create_run failed", exc_info=True)
        run = _NoopRun()
    try:
        yield run
    finally:
        if isinstance(run, _Run):
            try:
                error = run.attrs.pop("error", None)
                _client.update_run(run_id, outputs=run.attrs, error=error, end_time=datetime.now(UTC))
            except Exception:
                log.debug("langsmith update_run failed", exc_info=True)
