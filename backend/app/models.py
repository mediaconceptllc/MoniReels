"""Single source of truth for cross-cutting domain models (mirrored by hand in Flutter).

Timestamps are always float seconds — never a formatted string (see app.utils.timecode
for the only place conversion to HH:MM:SS.mmm / SRT / ASS is allowed to happen).
"""
from __future__ import annotations

import time
import uuid

from pydantic import BaseModel, Field

from app.timeline.models import Clip, Transition

SCHEMA_VERSION = 1


class VideoMeta(BaseModel):
    path: str
    duration_sec: float
    width: int
    height: int
    fps: float
    has_audio: bool
    codec: str
    thumbnail_path: str


class Word(BaseModel):
    start: float
    end: float
    text: str


class Segment(BaseModel):
    id: str
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[Word] = Field(default_factory=list)


class Transcript(BaseModel):
    language: str
    segments: list[Segment]
    full_text: str
    timings_estimated: bool = False


class ShortIdea(BaseModel):
    id: str
    title: str
    hook: str
    description: str
    start: float
    end: float


class KeepRange(BaseModel):
    start: float
    end: float
    reason: str


class YoutubePlan(BaseModel):
    title: str
    description: str
    ranges: list[KeepRange]
    total_duration: float


class Suggestions(BaseModel):
    shorts: list[ShortIdea]  # always exactly 3, enforced by post-validation
    youtube: YoutubePlan | None = None


class SubtitleStyle(BaseModel):
    enabled: bool = True
    font_family: str = "Arial"
    font_size: int = 42
    primary_color: str = "#FFFFFF"  # "#RRGGBB"
    outline_color: str = "#000000"
    outline_width: float = 2.0
    shadow: float = 0.0
    position: str = "bottom"  # "bottom" | "top" | "center"
    margin_v: int = 40


class ExportSettings(BaseModel):
    container: str = "mp4"  # "mp4" | "mov"
    orientation: str = "landscape"  # "landscape" | "portrait"
    portrait_fill: str = "blur"  # "blur" | "crop" | "pad"
    use_hwaccel: bool = True
    crf: int = 20
    preset: str = "medium"
    burn_subtitles: bool = False
    write_srt: bool = True


class Project(BaseModel):
    schema_version: int = SCHEMA_VERSION
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    video: VideoMeta | None = None
    transcript: Transcript | None = None
    suggestions: Suggestions | None = None
    clips: list[Clip] = Field(default_factory=list)
    transition: Transition = Field(default_factory=Transition)
    subtitle_style: SubtitleStyle = Field(default_factory=SubtitleStyle)
    export: ExportSettings = Field(default_factory=ExportSettings)


def migrate_project_dict(data: dict) -> dict:
    """Upgrade an on-disk project.json to the current schema_version in place.

    Stub: no migrations exist yet (schema_version is still 1). Add version-gated
    transforms here as the schema evolves; never mutate old on-disk files directly.
    """
    version = data.get("schema_version", 1)
    if version == SCHEMA_VERSION:
        return data
    # Future: if version == 1: data = _migrate_v1_to_v2(data); version = 2
    data["schema_version"] = SCHEMA_VERSION
    return data
