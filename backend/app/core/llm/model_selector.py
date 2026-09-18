"""Model configuration as data: one model per pipeline stage plus an ordered fallback chain."""

from __future__ import annotations

from enum import StrEnum

from app.config import Settings


class Stage(StrEnum):
    screen = "screen"
    agent = "agent"
    guard = "guard"
    narrative = "narrative"
    grouping = "grouping"


def models_for(settings: Settings, stage: Stage) -> list[str]:
    primary = {
        Stage.screen: settings.llm_model_screen,
        Stage.agent: settings.llm_model_agent,
        Stage.guard: settings.llm_model_guard,
        Stage.narrative: settings.llm_model_narrative,
        Stage.grouping: settings.llm_model_grouping,
    }[stage]
    chain = [primary]
    for m in settings.fallback_models:
        if m not in chain:
            chain.append(m)
    return chain


def temperature_for(settings: Settings, stage: Stage) -> float:
    # anything that becomes code or a verdict is deterministic
    if stage in (Stage.agent, Stage.guard, Stage.screen, Stage.grouping):
        return settings.llm_temperature_code
    return settings.llm_temperature_narrative
