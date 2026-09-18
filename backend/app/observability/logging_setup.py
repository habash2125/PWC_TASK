"""Structured JSON logging on named channels.

Channels: ``lens.request``, ``lens.generation``, ``lens.sql``, ``lens.chart``,
``lens.audit``.  Every line carries the correlation ids from
:mod:`app.observability.request_context`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.observability.request_context import current_context

CHANNELS = ("request", "generation", "sql", "chart", "audit")

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "channel": record.name,
            "msg": record.getMessage(),
        }
        payload.update({k: v for k, v in current_context().items() if v})
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for ch in CHANNELS:
        logging.getLogger(f"lens.{ch}").setLevel(level.upper())


def channel(name: str) -> logging.Logger:
    assert name in CHANNELS, name
    return logging.getLogger(f"lens.{name}")
