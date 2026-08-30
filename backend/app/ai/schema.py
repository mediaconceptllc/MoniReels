"""JSON schema for the LLM's structured output + Pydantic validation + post-validation.

Two layers, per spec:
1. Schema validation (`validate_llm_output`) — is the JSON shaped correctly. The
   raw shapes here mirror app.ai.prompts' CUT_SCHEMA/SHORT_SCHEMA/YOUTUBE_PLAN_SCHEMA
   exactly (that module owns the strict JSON schema actually sent to the API).
2. Post-validation (`postprocess_suggestions`) — resolves the model's segment
   indices back to real timestamps against the same segment list the prompt
   was built from, plus backend-enforced business rules the model cannot be
   trusted to get right on its own (exact short count, duration bounds,
   youtube-plan gating, range merging).
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.models import Cut, KeepRange, ShortIdea, Suggestions, YoutubePlan
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Matches app.ai.prompts.SYSTEM_PROMPT's stated cutting rule ("35-60 seconds.
# Never exceed 60") — kept here too since this is the backend backstop that
# actually enforces it, independent of whether the model followed the prompt.
MIN_SHORT_DURATION = 35.0
MAX_SHORT_DURATION = 60.0
MIN_CUTS = 3
MAX_CUTS = 5
REQUIRED_SHORT_COUNT = 3
VALID_ROLES = {"hook", "context", "proof", "payoff"}

YOUTUBE_MIN_VIDEO_DURATION_SEC = 1200.0  # 20 minutes
YOUTUBE_TARGET_DURATION_SEC = 600.0  # 10 minutes, per idea
YOUTUBE_TARGET_TOLERANCE = 0.10
REQUIRED_YOUTUBE_COUNT = 3  # only enforced when the video is >= YOUTUBE_MIN_VIDEO_DURATION_SEC
MERGE_GAP_SEC = 0.5

# The STT provider returns no word-level timestamps, so a segment's start/end is
# frequently an estimate (proportional-by-character/word split of a larger
# chunk, not a real detected boundary - see app.audio.vad_chunking). A cut
# resolved exactly on that estimate has a real chance of starting a beat
# late or ending a beat early relative to where the sentence actually falls
# in the audio, clipping the first/last word or losing setup context. This
# small outward pad is cheap insurance against that, not a fix for the
# underlying imprecision - a cut built from a badly-off estimate can still
# miss real context by more than this pads for.
CUT_PAD_SEC = 0.2

Segments = list[tuple[float, float, str]]  # app.ai.prompts.split_long_segments' output


class SuggestionValidationError(Exception):
    pass


# --------------------------------------------------------------------------
# Layer 1: shape validation (matches app.ai.prompts' CUT_SCHEMA/SHORT_SCHEMA/
# YOUTUBE_PLAN_SCHEMA, but re-checked here in case the provider's structured-
# output enforcement is imperfect or absent).
# --------------------------------------------------------------------------


class RawCut(BaseModel):
    start_index: int
    end_index: int
    role: str
    reason: str


class RawShort(BaseModel):
    title: str
    hook_text: str
    hook_quote: str
    cuts: list[RawCut]
    on_screen_texts: list[str] = Field(default_factory=list)
    b_roll: list[str] = Field(default_factory=list)
    caption: str
    hashtags: list[str] = Field(default_factory=list)
    why_it_works: str


class RawKeepRange(BaseModel):
    start_index: int
    end_index: int


class RawYoutubePlan(BaseModel):
    title: str
    throughline: str
    keep_ranges: list[RawKeepRange] = Field(default_factory=list)


class RawSuggestions(BaseModel):
    shorts: list[RawShort]
    # 0 items or exactly REQUIRED_YOUTUBE_COUNT (enforced in postprocess_suggestions,
    # since whether youtube is wanted at all depends on video duration, which this
    # shape-only layer doesn't know about).
    youtube: list[RawYoutubePlan] = Field(default_factory=list)


def validate_llm_output(raw_json: dict) -> RawSuggestions:
    try:
        return RawSuggestions.model_validate(raw_json)
    except Exception as e:  # noqa: BLE001 - re-raised as a typed, prompt-appendable error
        raise SuggestionValidationError(f"Response did not match the required JSON shape: {e}") from e


# --------------------------------------------------------------------------
# Layer 2: post-validation / index resolution / auto-correction of
# backend-enforced invariants.
# --------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _resolve_index(index: int, segments: Segments) -> tuple[float, float]:
    """Clamps an out-of-range segment index to the nearest valid one instead
    of failing the whole short over it — the model referencing index 412
    when only 400 segments exist is a near-miss, not the kind of thing worth
    losing an otherwise-good short over.
    """
    if not segments:
        raise SuggestionValidationError("No transcript segments to resolve indices against")
    clamped = max(0, min(index, len(segments) - 1))
    return segments[clamped][0], segments[clamped][1]


def _resolve_cut(raw: RawCut, segments: Segments, duration_sec: float) -> Cut:
    lo, hi = min(raw.start_index, raw.end_index), max(raw.start_index, raw.end_index)
    start, _ = _resolve_index(lo, segments)
    _, end = _resolve_index(hi, segments)
    if end <= start:
        raise SuggestionValidationError(
            f"Cut [{raw.start_index}, {raw.end_index}] resolves to a zero/negative-length range"
        )
    start = _clamp(start - CUT_PAD_SEC, 0.0, duration_sec)
    end = _clamp(end + CUT_PAD_SEC, 0.0, duration_sec)
    role = raw.role if raw.role in VALID_ROLES else "context"
    return Cut(start=start, end=end, role=role, reason=raw.reason)


def _build_short(raw: RawShort, segments: Segments, duration_sec: float) -> ShortIdea:
    if not MIN_CUTS <= len(raw.cuts) <= MAX_CUTS:
        raise SuggestionValidationError(
            f"Short {raw.title!r} has {len(raw.cuts)} cuts, must be {MIN_CUTS}-{MAX_CUTS}"
        )

    cuts = [_resolve_cut(c, segments, duration_sec) for c in raw.cuts]
    total = sum(c.end - c.start for c in cuts)
    if not MIN_SHORT_DURATION <= total <= MAX_SHORT_DURATION:
        raise SuggestionValidationError(
            f"Short {raw.title!r}: total duration {total:.0f}s, must be "
            f"{MIN_SHORT_DURATION:.0f}-{MAX_SHORT_DURATION:.0f}s"
        )
    if cuts[-1].role != "payoff":
        raise SuggestionValidationError(
            f"Short {raw.title!r}: last cut has role {cuts[-1].role!r}, must be 'payoff'"
        )
    if not any(c.role == "hook" for c in cuts):
        raise SuggestionValidationError(f"Short {raw.title!r}: no cut with role 'hook'")

    return ShortIdea(
        id=uuid.uuid4().hex,
        title=raw.title,
        hook_text=raw.hook_text,
        hook_quote=raw.hook_quote,
        cuts=cuts,
        on_screen_texts=raw.on_screen_texts,
        b_roll=raw.b_roll,
        caption=raw.caption,
        hashtags=raw.hashtags,
        why_it_works=raw.why_it_works,
    )


def _merge_ranges(ranges: list[tuple[float, float]], duration_sec: float) -> list[KeepRange]:
    clamped = []
    for start, end in ranges:
        start = _clamp(start, 0.0, duration_sec)
        end = _clamp(end, 0.0, duration_sec)
        if end > start:
            clamped.append((start, end))

    clamped.sort(key=lambda t: t[0])

    merged: list[list[float]] = []
    for start, end in clamped:
        if merged and start - merged[-1][1] < MERGE_GAP_SEC:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return [KeepRange(start=s, end=e) for s, e in merged]


def _build_youtube_plan(raw: RawYoutubePlan, segments: Segments, duration_sec: float) -> YoutubePlan:
    resolved: list[tuple[float, float]] = []
    for kr in raw.keep_ranges:
        lo, hi = min(kr.start_index, kr.end_index), max(kr.start_index, kr.end_index)
        start, _ = _resolve_index(lo, segments)
        _, end = _resolve_index(hi, segments)
        if end > start:
            resolved.append((start, end))

    ranges = _merge_ranges(resolved, duration_sec)
    total = sum(r.end - r.start for r in ranges)
    low = YOUTUBE_TARGET_DURATION_SEC * (1 - YOUTUBE_TARGET_TOLERANCE)
    high = YOUTUBE_TARGET_DURATION_SEC * (1 + YOUTUBE_TARGET_TOLERANCE)
    if not (low <= total <= high):
        logger.warning(
            "YouTube plan %r total duration %.1fs is outside the %.0fs +/-%.0f%% target",
            raw.title, total, YOUTUBE_TARGET_DURATION_SEC, YOUTUBE_TARGET_TOLERANCE * 100,
        )
    return YoutubePlan(title=raw.title, throughline=raw.throughline, ranges=ranges, total_duration=total)


def postprocess_suggestions(raw: RawSuggestions, segments: Segments, duration_sec: float) -> Suggestions:
    if len(raw.shorts) != REQUIRED_SHORT_COUNT:
        raise SuggestionValidationError(
            f"Expected exactly {REQUIRED_SHORT_COUNT} shorts, got {len(raw.shorts)}"
        )

    shorts = [_build_short(s, segments, duration_sec) for s in raw.shorts]

    # Below the duration gate, youtube is always [] regardless of what the
    # model returned - a short video should never fail suggestion generation
    # over youtube being wrong.
    youtube: list[YoutubePlan] = []
    if duration_sec > YOUTUBE_MIN_VIDEO_DURATION_SEC:
        if len(raw.youtube) != REQUIRED_YOUTUBE_COUNT:
            raise SuggestionValidationError(
                f"Expected exactly {REQUIRED_YOUTUBE_COUNT} youtube ideas, got {len(raw.youtube)}"
            )
        youtube = [_build_youtube_plan(plan, segments, duration_sec) for plan in raw.youtube]

    return Suggestions(shorts=shorts, youtube=youtube)
