"""Request/response models.  ``extra="forbid"`` and explicit length bounds on every string."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Out(BaseModel):
    """Response models are built from ORM rows via ``from_attributes``; never the rows themselves."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)
