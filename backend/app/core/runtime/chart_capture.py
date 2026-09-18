"""Chart capture: the model writes ordinary Plotly and calls ``fig.show()``.

``show()`` is patched to serialise the *real* figure object to ``{data, layout}``,
which is then validated against a Pydantic envelope.  No allow-list of chart
kinds is needed — every Plotly trace type works — and nothing is asked of the
model "as JSON".
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_SPEC_BYTES = 2_000_000
MAX_POINTS_PER_TRACE = 20_000
DEFAULT_TITLE = "Untitled chart"


class ChartSpecEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    layout: dict[str, Any] = Field(default_factory=dict)


class CapturedChart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    spec: ChartSpecEnvelope
    warnings: list[str] = Field(default_factory=list)


def _extract_title(layout: dict[str, Any]) -> str | None:
    title = layout.get("title")
    if isinstance(title, dict):
        text = title.get("text")
    else:
        text = title
    if isinstance(text, str) and text.strip():
        return text.strip()[:200]
    return None


def figure_to_capture(fig: Any, index: int) -> CapturedChart:
    """Serialises a live figure.  Raises ``ValueError`` with a message the model can act on."""
    import plotly.io as pio

    raw = json.loads(pio.to_json(fig, validate=True, remove_uids=True))
    payload = json.dumps(raw)
    if len(payload) > MAX_SPEC_BYTES:
        raise ValueError(
            f"figure {index} serialises to {len(payload) // 1000} kB (limit {MAX_SPEC_BYTES // 1000} kB); aggregate the data before plotting"
        )
    try:
        envelope = ChartSpecEnvelope(data=raw.get("data", []), layout=raw.get("layout", {}))
    except ValidationError as exc:
        raise ValueError(f"figure {index} is not a valid chart spec: {exc.errors()[0].get('msg', 'invalid')}") from None
    warnings: list[str] = []
    for i, trace in enumerate(envelope.data):
        for axis in ("x", "y", "z", "values", "labels"):
            v = trace.get(axis)
            if isinstance(v, list) and len(v) > MAX_POINTS_PER_TRACE:
                raise ValueError(
                    f"figure {index}, trace {i}: {len(v)} points on '{axis}' exceeds {MAX_POINTS_PER_TRACE}; aggregate first"
                )
    title = _extract_title(envelope.layout)
    if title is None:
        title = f"{DEFAULT_TITLE} {index}"
        warnings.append(
            f"figure {index} has no title; set fig.update_layout(title='...') — a descriptive title is required"
        )
    return CapturedChart(title=title, spec=envelope, warnings=warnings)


def validate_stored_spec(spec: dict[str, Any]) -> ChartSpecEnvelope:
    """Used when a spec is read back from the database before it is sent to a client."""
    return ChartSpecEnvelope.model_validate(spec)
