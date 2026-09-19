"""Two-sided injection resistance.

Inbound: the question is screened by a deterministic pre-screen (cheap, catches
the obvious) and then by the guard model, which returns a structured verdict.
Outbound: the final narrative is filtered so that instruction-like text that
arrived through database values is neutralised before display.

User text is always wrapped as a delimited *data* block; it is never
concatenated into an instruction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.observability import metrics

log = logging.getLogger("lens.generation")

Category = Literal["injection", "scope_evasion", "sql_attack", "personal_data", "off_topic", "none"]


class ScreenVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["allow", "block"]
    category: Category = "none"
    reason: str = Field(max_length=300)
    redacted_question: str | None = Field(default=None, max_length=4000)


@dataclass(frozen=True, slots=True)
class ScreenResult:
    allowed: bool
    category: str
    reason: str
    question: str  # possibly redacted
    stage: Literal["prescreen", "model"]


# Patterns that are never a legitimate analytics question.  Kept deliberately narrow: the model does the
# nuanced work, this layer removes the need to spend a model call on the blatant cases.
_PRESCREEN = [
    (
        re.compile(r"ignore\s+(all\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)", re.I),
        "injection",
    ),
    (
        re.compile(
            r"\b(you\s+are\s+now|act\s+as|pretend\s+to\s+be)\s+(a|an|the)?\s*(system|admin|root|developer|dba)\b", re.I
        ),
        "injection",
    ),
    (re.compile(r"\b(system|developer)\s+(prompt|override|mode)\b", re.I), "injection"),
    (
        re.compile(
            r"\b(reveal|print|show|dump)\b.{0,40}\b(system\s+prompt|instructions|api\s+key|password|credentials|env(ironment)?\s+var)",
            re.I,
        ),
        "injection",
    ),
    (re.compile(r"\b(drop|truncate|alter)\s+(table|view|schema|database)\b", re.I), "sql_attack"),
    (re.compile(r"\b(delete\s+from|insert\s+into|update\s+\w+\s+set)\b", re.I), "sql_attack"),
    (
        re.compile(
            r"\b(pg_sleep|pg_read_file|dblink|lo_import|information_schema|pg_catalog|sqlite_master|sqlite_schema|"
            r"load_extension|readfile|writefile)\b",
            re.I,
        ),
        "sql_attack",
    ),
    (re.compile(r"\b(pragma\s+\w+|attach\s+database)\b", re.I), "sql_attack"),
    (re.compile(r"select\s+\*\s+from\s+(users?|app_user|passwords?|credentials)\b", re.I), "sql_attack"),
    (
        re.compile(r"\b(base\s+tables?|underlying\s+tables?)\b.{0,30}\b(bypass|without|instead\s+of)\b", re.I),
        "scope_evasion",
    ),
    (
        re.compile(
            r"\b(other|all)\s+(users'?|clients'?|customers'?|regions'?|warehouses'?)\s+(data|rows|records)\b.{0,40}\b(i\s+am\s+not|not\s+allowed|bypass|ignore)",
            re.I,
        ),
        "scope_evasion",
    ),
]


def prescreen(question: str) -> tuple[str, str] | None:
    for pattern, category in _PRESCREEN:
        if pattern.search(question):
            return category, f"matched pre-screen pattern for {category}"
    return None


async def screen_question(question: str) -> ScreenResult:
    hit = prescreen(question)
    if hit is not None:
        category, reason = hit
        metrics.guard_blocks.labels("injection", category).inc()
        return ScreenResult(False, category, reason, question, "prescreen")

    from app.core.llm.client import get_llm
    from app.core.llm.model_selector import Stage
    from app.db.seed.analytics_catalog import DATASET
    from app.prompts import get_prompt

    prompt = get_prompt("injection_screen")
    resp = await get_llm().complete(
        Stage.screen,
        [
            {"role": "system", "content": prompt.render(topics=DATASET.topics)},
            {"role": "user", "content": "<question>\n" + question + "\n</question>"},
        ],
        response_model=ScreenVerdict,
        max_tokens=1500,
        prompt_version_id=prompt.version_id,
    )
    verdict = resp.parsed
    assert isinstance(verdict, ScreenVerdict)
    if verdict.verdict == "block":
        metrics.guard_blocks.labels("injection", verdict.category).inc()
        return ScreenResult(False, verdict.category, verdict.reason, question, "model")
    cleaned = verdict.redacted_question.strip() if verdict.redacted_question else question
    return ScreenResult(True, "none", verdict.reason, cleaned or question, "model")


# ── outbound filter ──────────────────────────────────────────────────────────

_OUTBOUND = [
    re.compile(r"ignore\s+(all\s+|the\s+)?(previous|prior|above|earlier)\s+instructions?[^.\n]*", re.I),
    re.compile(r"\b(system|developer)\s+(note|prompt|override|message)\s*:[^.\n]*", re.I),
    re.compile(r"select\s+\*\s+from\s+\w+", re.I),
    re.compile(r"\b(your|the)\s+password\s+is\s+\S+", re.I),
    re.compile(r"\bhunter2\b", re.I),
]
_REDACTED = "[redacted: instruction-like text found in data]"


def filter_narrative(text: str) -> tuple[str, int]:
    """Neutralises instruction-like fragments that came back through data.  Returns ``(text, replacements)``."""
    count = 0
    for pattern in _OUTBOUND:
        text, n = pattern.subn(_REDACTED, text)
        count += n
    return text, count


def wrap_data(tag: str, content: str) -> str:
    """Delimits untrusted content as data.  The closing tag inside content is escaped so it cannot terminate the block."""
    safe = content.replace(f"</{tag}>", f"</{tag} >")
    return f"<{tag}>\n{safe}\n</{tag}>"
