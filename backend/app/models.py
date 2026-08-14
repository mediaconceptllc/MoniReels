"""Single source of truth for cross-cutting domain models (mirrored by hand in Flutter).

Timestamps are always float seconds — never a formatted string (see app.utils.timecode
for the only place conversion to HH:MM:SS.mmm / SRT / ASS is allowed to happen).
"""
from __future__ import annotations

import time
import uuid

from pydantic import BaseModel, Field

from app.timeline.models import Clip, Transition

SCHEMA_VERSION = 3


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


class Cut(BaseModel):
    """One piece of a ShortIdea's edit — 3-5 of these, each a separate
    non-contiguous range from the source video, assembled in the order
    given (not necessarily chronological order in the source)."""

    start: float
    end: float
    role: str  # "hook" | "context" | "proof" | "payoff"
    reason: str


class ShortIdea(BaseModel):
    id: str
    title: str
    hook_text: str  # on-screen text for the first 3 seconds
    hook_quote: str  # verbatim transcript substring the hook is drawn from
    cuts: list[Cut]  # 3-5, ordered as they appear in the final edit
    on_screen_texts: list[str] = Field(default_factory=list)
    b_roll: list[str] = Field(default_factory=list)
    caption: str
    hashtags: list[str] = Field(default_factory=list)
    why_it_works: str


class KeepRange(BaseModel):
    start: float
    end: float
    reason: str = ""


class YoutubePlan(BaseModel):
    title: str
    throughline: str
    ranges: list[KeepRange]
    total_duration: float


class Suggestions(BaseModel):
    shorts: list[ShortIdea]  # always exactly 3, enforced by post-validation
    # 0 items (video < 20min) or exactly 3 (video >= 20min), enforced by
    # post-validation. Empty list, not None, when not wanted.
    youtube: list[YoutubePlan] = Field(default_factory=list)


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


def _migrate_v1_to_v2(data: dict) -> dict:
    """v1 suggestions.youtube was a single object-or-null; v2 is always a list
    (0 or 3 items) to support multiple independent YouTube-highlight ideas.
    """
    suggestions = data.get("suggestions")
    if suggestions is not None:
        youtube = suggestions.get("youtube")
        if youtube is None:
            suggestions["youtube"] = []
        elif isinstance(youtube, dict):
            suggestions["youtube"] = [youtube]
    return data


def _migrate_v2_to_v3(data: dict) -> dict:
    """v2 ShortIdea/YoutubePlan used a flat single-range shape (title/hook/
    description/start/end, and youtube.description); v3 replaces shorts with
    multi-cut edits (hook_text/hook_quote/cuts/on_screen_texts/b_roll/caption/
    hashtags/why_it_works) and renames youtube.description to .throughline.
    There's no principled way to synthesize the new required short fields
    from old data, so an old-shape suggestions block is simply cleared - the
    user re-runs suggestion generation. (A youtube-only re-shape would be
    possible, but shorts and youtube share one `suggestions` block and the
    shorts side can't be salvaged, so the whole block goes.)
    """
    suggestions = data.get("suggestions")
    if suggestions is None:
        return data
    shorts = suggestions.get("shorts") or []
    if any("cuts" not in s for s in shorts):
        data["suggestions"] = None
        return data
    for plan in suggestions.get("youtube") or []:
        if "throughline" not in plan and "description" in plan:
            plan["throughline"] = plan.pop("description")
    return data


def migrate_project_dict(data: dict) -> dict:
    """Upgrade an on-disk project.json to the current schema_version in place.

    Add version-gated transforms here as the schema evolves; never mutate old
    on-disk files directly (callers only ever hold the migrated dict in memory).
    """
    version = data.get("schema_version", 1)
    if version == SCHEMA_VERSION:
        return data
    if version == 1:
        data = _migrate_v1_to_v2(data)
        version = 2
    if version == 2:
        data = _migrate_v2_to_v3(data)
        version = 3
    data["schema_version"] = SCHEMA_VERSION
    return data
