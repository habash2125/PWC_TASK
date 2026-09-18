"""Redaction of sensitive values and secrets in anything that reaches a log, a trace or the model.

Logs record SQL and row counts; values in columns flagged sensitive are
replaced.  DSNs and API keys are masked defensively wherever a free-text
message might carry one.
"""

from __future__ import annotations

import re
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:\s/]+:)([^@\s]+)(@)", re.I),  # password inside a DSN
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]{10,}"),  # API keys
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.I),
]
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def mask_secrets(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub(r"\1***\3", text)
    text = _SECRET_PATTERNS[1].sub(r"\1***", text)
    text = _SECRET_PATTERNS[2].sub(r"\1***", text)
    return text


def redact_row(columns: list[str], row: tuple[Any, ...], sensitive: set[str]) -> list[Any]:
    lowered = [c.lower() for c in columns]
    return ["<redacted>" if lowered[i] in sensitive else v for i, v in enumerate(row)]


def redact_preview(
    columns: list[str], rows: list[tuple[Any, ...]], sensitive: set[str], limit: int = 5
) -> list[list[Any]]:
    return [redact_row(columns, r, sensitive) for r in rows[:limit]]


def redact_free_text(text: str) -> str:
    """Used on log lines that might carry cell values: masks e-mails and secrets."""
    return _EMAIL.sub("<email>", mask_secrets(text))
