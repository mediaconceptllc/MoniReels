"""Timeline-specific domain models: the clips a render is built from."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Clip(BaseModel):
    id: str
    source_path: str
    start: float  # seconds, inclusive
    end: float  # seconds, exclusive
    order: int
    transition_in: str | None = None  # per-clip override of the project-wide transition


class Transition(BaseModel):
    type: str = "Cross Fade"  # UI name; resolved against transition.registry at render time
    duration: float = Field(default=0.5, ge=0.0, le=2.0)
