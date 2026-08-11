"""JSON schema for the LLM's structured output + Pydantic validation + post-validation.

Two layers, per spec:
1. Schema validation (`validate_llm_output`) — is the JSON shaped correctly.
2. Post-validation (`postprocess_suggestions`) — backend-enforced business rules
   that the model cannot be trusted to get right on its own (exact short count,
   timestamp clamping, duration bounds, youtube-plan gating, range merging).
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.models import KeepRange, ShortIdea, Suggestions, YoutubePlan
from app.utils.logging import get_logger

logger = get_logger(__name__)

MIN_SHORT_DURATION = 15.0
MAX_SHORT_DURATION = 90.0
REQUIRED_SHORT_COUNT = 3

YOUTUBE_MIN_VIDEO_DURATION_SEC = 1200.0  # 20 minutes
YOUTUBE_TARGET_DURATION_SEC = 600.0  # 10 minutes
YOUTUBE_TARGET_TOLERANCE = 0.10
MERGE_GAP_SEC = 0.5


class SuggestionValidationError(Exception):
    pass


# --------------------------------------------------------------------------
# Layer 1: shape validation (matches the JSON schema sent to the API, but
# re-checked here in case the provider's structured-output enforcement is
# imperfect or absent).
# --------------------------------------------------------------------------


class RawShort(BaseModel):
    title: str
    hook: str
    description: str
    start: float
    end: float


class RawKeepRange(BaseModel):
    start: float
    end: float
    reason: str


class RawYoutubePlan(BaseModel):
    title: str
    description: str
    ranges: list[RawKeepRange]


class RawSuggestions(BaseModel):
    shorts: list[RawShort]
    youtube: RawYoutubePlan | None = None


SUGGESTIONS_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "shorts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "description": {"type": "string"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                },
                "required": ["title", "hook", "description", "start", "end"],
            },
        },
        "youtube": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "ranges": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "start": {"type": "number"},
                                    "end": {"type": "number"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["start", "end", "reason"],
                            },
                        },
                    },
                    "required": ["title", "description", "ranges"],
                },
                {"type": "null"},
            ]
        },
    },
    "required": ["shorts", "youtube"],
}


def validate_llm_output(raw_json: dict) -> RawSuggestions:
    try:
        return RawSuggestions.model_validate(raw_json)
    except Exception as e:  # noqa: BLE001 - re-raised as a typed, prompt-appendable error
        raise SuggestionValidationError(f"Response did not match the required JSON shape: {e}") from e


# --------------------------------------------------------------------------
# Layer 2: post-validation / auto-correction of backend-enforced invariants.
# --------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _clamp_short(raw: RawShort, duration_sec: float) -> ShortIdea:
    start = _clamp(raw.start, 0.0, duration_sec)
    end = _clamp(raw.end, 0.0, duration_sec)
    if end <= start:
        raise SuggestionValidationError(
            f"Short {raw.title!r} has a zero/negative-length range after clamping to video duration"
        )

    span = end - start
    if span > MAX_SHORT_DURATION:
        end = start + MAX_SHORT_DURATION
    elif span < MIN_SHORT_DURATION:
        end = min(duration_sec, start + MIN_SHORT_DURATION)
        if end - start < MIN_SHORT_DURATION:
            raise SuggestionValidationError(
                f"Short {raw.title!r} cannot reach the {MIN_SHORT_DURATION}s minimum "
                f"within the video's {duration_sec}s duration"
            )

    return ShortIdea(
        id=uuid.uuid4().hex, title=raw.title, hook=raw.hook, description=raw.description, start=start, end=end
    )


def _merge_ranges(ranges: list[RawKeepRange], duration_sec: float) -> list[KeepRange]:
    clamped = []
    for r in ranges:
        start = _clamp(r.start, 0.0, duration_sec)
        end = _clamp(r.end, 0.0, duration_sec)
        if end > start:
            clamped.append((start, end, r.reason))

    clamped.sort(key=lambda t: t[0])

    merged: list[list] = []
    for start, end, reason in clamped:
        if merged and start - merged[-1][1] < MERGE_GAP_SEC:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = f"{merged[-1][2]}; {reason}"
        else:
            merged.append([start, end, reason])

    return [KeepRange(start=s, end=e, reason=r) for s, e, r in merged]


def postprocess_suggestions(raw: RawSuggestions, duration_sec: float) -> Suggestions:
    if len(raw.shorts) != REQUIRED_SHORT_COUNT:
        raise SuggestionValidationError(
            f"Expected exactly {REQUIRED_SHORT_COUNT} shorts, got {len(raw.shorts)}"
        )

    shorts = [_clamp_short(s, duration_sec) for s in raw.shorts]

    youtube: YoutubePlan | None = None
    if duration_sec > YOUTUBE_MIN_VIDEO_DURATION_SEC and raw.youtube is not None:
        ranges = _merge_ranges(raw.youtube.ranges, duration_sec)
        if ranges:
            total = sum(r.end - r.start for r in ranges)
            low = YOUTUBE_TARGET_DURATION_SEC * (1 - YOUTUBE_TARGET_TOLERANCE)
            high = YOUTUBE_TARGET_DURATION_SEC * (1 + YOUTUBE_TARGET_TOLERANCE)
            if not (low <= total <= high):
                logger.warning(
                    "YouTube plan total duration %.1fs is outside the %.0fs +/-%.0f%% target",
                    total, YOUTUBE_TARGET_DURATION_SEC, YOUTUBE_TARGET_TOLERANCE * 100,
                )
            youtube = YoutubePlan(
                title=raw.youtube.title,
                description=raw.youtube.description,
                ranges=ranges,
                total_duration=total,
            )

    return Suggestions(shorts=shorts, youtube=youtube)
