"""Resolves ``<chart N>`` anchors in the narrative to real charts, positionally.

An anchor that points at a chart that does not exist is removed; a duplicate
anchor is removed; a chart that was never anchored is appended.  Nothing is
dropped.
"""

from __future__ import annotations

import re

_ANCHOR = re.compile(r"<chart\s+(\d+)\s*>", re.IGNORECASE)


def place_charts(markdown: str, chart_count: int) -> tuple[str, list[int]]:
    """Returns the normalised markdown and the 1-based order in which charts appear."""
    seen: list[int] = []

    def _resolve(m: re.Match[str]) -> str:
        n = int(m.group(1))
        if 1 <= n <= chart_count and n not in seen:
            seen.append(n)
            return f"\n\n<chart {n}>\n\n"  # always on its own line, wherever the model put it
        return ""

    text = _ANCHOR.sub(_resolve, markdown)
    for n in range(1, chart_count + 1):
        if n not in seen:
            text = text.rstrip() + f"\n\n<chart {n}>\n"
            seen.append(n)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text, seen
