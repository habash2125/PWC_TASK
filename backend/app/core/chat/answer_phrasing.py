"""Narrative phrasing with a small/fast model.  Receives chart TITLES only, never specs."""

from __future__ import annotations

import logging

from app.api.errors import ProviderUnavailable
from app.core.llm.budgets import Usage
from app.core.llm.client import get_llm
from app.core.llm.model_selector import Stage
from app.core.security.prompt_injection import filter_narrative, wrap_data
from app.prompts import get_prompt

log = logging.getLogger("lens.generation")


async def phrase_answer(*, question: str, findings: str, chart_titles: list[str]) -> tuple[str, Usage, str]:
    """Returns ``(markdown, usage, prompt_version_id)``.  Falls back to the findings if the model is unavailable."""
    prompt = get_prompt("narrative")
    titles = "\n".join(f"<chart {i}>: {t}" for i, t in enumerate(chart_titles, start=1)) or "(no charts)"
    user = "\n\n".join(
        [
            wrap_data("question", question),
            wrap_data("findings", findings or "(no findings)"),
            wrap_data("charts", titles),
        ]
    )
    try:
        resp = await get_llm().complete(
            Stage.narrative,
            [{"role": "system", "content": prompt.text}, {"role": "user", "content": user}],
            max_tokens=1500,
            prompt_version_id=prompt.version_id,
        )
    except ProviderUnavailable:
        # degrade honestly: the findings are already grounded; charts are appended by placement
        text, _ = filter_narrative(findings or "The analysis completed but no narrative could be generated.")
        return text, Usage(), prompt.version_id
    text, replaced = filter_narrative(resp.text.strip())
    if replaced:
        log.warning("narrative filtered", extra={"replacements": replaced})
    return text, resp.usage, prompt.version_id
