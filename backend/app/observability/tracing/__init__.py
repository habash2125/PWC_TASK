"""OpenTelemetry tracing exported into the ``trace_span`` table.

Spans are recorded by a ``BatchSpanProcessor`` on its own thread and handed to
an asyncio task on the main loop that writes them with the app-db engine.
Every failure inside this module is swallowed by construction: observability
must never break the request.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from app.config import Settings
from app.observability.request_context import trace_id_var

log = logging.getLogger("lens.request")

_provider: TracerProvider | None = None
_exporter: AsyncDbSpanExporter | None = None


def _ns_to_dt(ns: int | None) -> datetime | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=UTC)


def _serialisable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


class AsyncDbSpanExporter(SpanExporter):
    """Bridges the exporter thread to the event loop; writes rows with the async engine."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[list[dict[str, Any]]] | None = None
        self._task: asyncio.Task[None] | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=1000)
        self._task = loop.create_task(self._writer())

    async def _writer(self) -> None:
        from sqlalchemy import text

        from app.db.session import get_engine

        assert self._queue is not None
        stmt = text(
            """
            INSERT INTO trace_span (span_id, trace_id, parent_span_id, name, start_ts, end_ts, duration_ms, status, attributes)
            VALUES (:span_id, :trace_id, :parent_span_id, :name, :start_ts, :end_ts, :duration_ms, :status, CAST(:attributes AS jsonb))
            ON CONFLICT (span_id) DO NOTHING
            """
        )
        while True:
            batch = await self._queue.get()
            try:
                async with get_engine().begin() as conn:
                    await conn.execute(stmt, batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # observability must never break anything
                log.debug("span export failed", extra={"error": type(exc).__name__})
            finally:
                self._queue.task_done()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._loop is None or self._queue is None or self._loop.is_closed():
            return SpanExportResult.FAILURE
        rows: list[dict[str, Any]] = []
        for span in spans:
            ctx = span.get_span_context()
            parent = span.parent
            attrs = {k: _serialisable(v) for k, v in (span.attributes or {}).items()}
            for event in span.events or ():
                attrs.setdefault("events", []).append(
                    {
                        "name": event.name,
                        "attributes": {k: _serialisable(v) for k, v in (event.attributes or {}).items()},
                    }
                )
            start = _ns_to_dt(span.start_time)
            end = _ns_to_dt(span.end_time)
            duration_ms = int((span.end_time - span.start_time) / 1e6) if span.end_time and span.start_time else None
            rows.append(
                {
                    "span_id": f"{ctx.span_id:016x}",
                    "trace_id": f"{ctx.trace_id:032x}",
                    "parent_span_id": f"{parent.span_id:016x}" if parent else None,
                    "name": span.name,
                    "start_ts": start,
                    "end_ts": end,
                    "duration_ms": duration_ms,
                    "status": span.status.status_code.name if span.status else "UNSET",
                    "attributes": json.dumps(attrs, default=str),
                }
            )
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, rows)
        except Exception:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def drain(self) -> None:
        if self._queue is not None:
            await self._queue.join()


def setup_tracing(settings: Settings) -> None:
    global _provider, _exporter
    if _provider is not None:
        return
    _exporter = AsyncDbSpanExporter()
    try:
        _exporter.bind(asyncio.get_running_loop())
    except RuntimeError:
        pass
    _provider = TracerProvider(resource=Resource.create({"service.name": "lens-api"}))
    _provider.add_span_processor(
        BatchSpanProcessor(_exporter, schedule_delay_millis=settings.otel_batch_delay_ms, max_export_batch_size=256)
    )
    trace.set_tracer_provider(_provider)


async def shutdown_tracing() -> None:
    global _provider, _exporter
    if _provider is not None:
        try:
            _provider.force_flush(timeout_millis=2000)
            if _exporter is not None:
                await asyncio.wait_for(_exporter.drain(), timeout=3)
        except Exception:
            pass
        _provider.shutdown()
    _provider = None
    _exporter = None


async def flush_spans() -> None:
    """Force everything buffered into the table (used by tests and the trace endpoint)."""
    if _provider is not None:
        await asyncio.to_thread(_provider.force_flush, 1000)
    if _exporter is not None:
        try:
            await asyncio.wait_for(_exporter.drain(), timeout=3)
        except Exception:
            pass


async def retention_sweep(days: int) -> int:
    """Deletes spans older than the retention window.  Called at startup and then hourly."""
    from sqlalchemy import text

    from app.db.session import get_engine

    try:
        async with get_engine().begin() as conn:
            result = await conn.execute(
                text("DELETE FROM trace_span WHERE start_ts < now() - make_interval(days => :days)"),
                {"days": int(days)},
            )
            return int(result.rowcount or 0)
    except Exception as exc:  # retention must never break the service
        log.debug("retention sweep failed", extra={"error": type(exc).__name__})
        return 0


async def retention_loop(days: int, interval_seconds: int = 3600) -> None:
    while True:
        deleted = await retention_sweep(days)
        if deleted:
            log.info("trace retention sweep", extra={"deleted_spans": deleted, "retention_days": days})
        await asyncio.sleep(interval_seconds)


def tracer() -> trace.Tracer:
    return trace.get_tracer("lens")


def span_trace_id(span: trace.Span) -> str | None:
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return f"{ctx.trace_id:032x}"


@contextmanager
def root_span(name: str, **attributes: Any):
    """Starts a brand-new trace (fresh context) and points ``trace_id_var`` at it.

    Used by the request middleware and by each tile refresh, which is its own trace.
    """
    with tracer().start_as_current_span(name, context=Context()) as span:
        tid = span_trace_id(span)
        token = trace_id_var.set(tid) if tid else None
        try:
            for k, v in attributes.items():
                _safe_set(span, k, v)
            yield span
        finally:
            if token is not None:
                trace_id_var.reset(token)


@contextmanager
def stage_span(name: str, **attributes: Any):
    """A child span for one pipeline stage; failures in attribute handling are ignored."""
    with tracer().start_as_current_span(name) as span:
        for k, v in attributes.items():
            _safe_set(span, k, v)
        yield span


def _safe_set(span: trace.Span, key: str, value: Any) -> None:
    try:
        if value is None:
            return
        if isinstance(value, (dict, list)):
            value = json.dumps(value, default=str)[:4000]
        span.set_attribute(key, value)
    except Exception:
        pass


def set_attributes(span: trace.Span, **attributes: Any) -> None:
    for k, v in attributes.items():
        _safe_set(span, k, v)
