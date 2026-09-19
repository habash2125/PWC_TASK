"""Lightweight table-relevance selection: narrows the schema-context *prompt* only.

This call runs on every turn but is a cost optimisation, not a security control: on any failure
(provider error, timeout, an empty or all-invalid list of view names) it fails OPEN and the caller
falls back to the full, unfiltered schema. It never touches the ``AllowList`` used for enforcement —
the guard and scope binding always see every enabled view, regardless of this filter.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

from app.core.sql.schema_context import AllowList

log = logging.getLogger("lens.generation")

_ID_SUFFIX = "_id"


class TableSelection(BaseModel):
    """Structured output of the LLM selector — never scraped from free text."""

    model_config = ConfigDict(extra="forbid")
    views: list[str]
    reason: str = Field(max_length=300, default="")


def render_lightweight_catalog(allow: AllowList) -> str:
    """Name + one-line description + column NAMES only — no types, descriptions or business rules."""
    parts = []
    for name in sorted(allow.views):
        v = allow.views[name]
        cols = ", ".join(c[0] for c in v.columns)
        parts.append(f"VIEW {v.name} — {v.description}\n  columns: {cols}")
    return "\n".join(parts)


def build_relations(allow: AllowList) -> str:
    """Deterministic join hints: pairs of views sharing an id-like column name.

    Excludes the row-level scope columns (e.g. ``region_id``) — almost every fact view carries one,
    and including it would collapse the graph into a single clique.
    """
    exclude = {c.lower() for c in allow.scope_columns.values()}
    id_cols = {
        name: {c[0].lower() for c in v.columns if c[0].lower().endswith(_ID_SUFFIX) and c[0].lower() not in exclude}
        for name, v in allow.views.items()
    }
    names = sorted(id_cols)
    lines = []
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            shared = sorted(id_cols[a] & id_cols[b])
            if shared:
                lines.append(f"- {allow.views[a].name} <-> {allow.views[b].name}: {', '.join(shared)}")
    return "\n".join(lines) if lines else "- (no shared identifier columns found)"


async def select_views(question: str, allow: AllowList) -> tuple[set[str] | None, str | None]:
    """Returns ``(selected view names | None, prompt_version_id)``.

    ``None`` means "render the full schema" — used both when the model call fails and when its
    answer is unusable (empty, or no returned name matches an allow-listed view).
    """
    from app.core.llm.client import get_llm
    from app.core.llm.model_selector import Stage
    from app.core.security.prompt_injection import wrap_data
    from app.prompts import get_prompt

    prompt = get_prompt("table_selector")
    system = prompt.render(catalog=render_lightweight_catalog(allow), relations=build_relations(allow))
    user = wrap_data("question", question)
    try:
        resp = await get_llm().complete(
            Stage.table_select,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_model=TableSelection,
            max_tokens=1000,
            prompt_version_id=prompt.version_id,
        )
        parsed = resp.parsed
        assert isinstance(parsed, TableSelection)
    except Exception as exc:  # never fail-closed: a cost optimisation, not a guard
        log.warning("table selection failed, rendering full schema", extra={"error": type(exc).__name__})
        return None, prompt.version_id
    selected = {name.lower() for name in parsed.views if name.lower() in allow.names}
    return (selected or None), prompt.version_id
