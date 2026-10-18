"""xfade/acrossfade filtergraph construction for joining normalized clips.

Every clip going into this must already share identical width, height, fps,
SAR, and pixel format (video) and sample rate/channel layout (audio) — that
normalization happens per-clip in app.export.pipeline before this ever runs.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class TransitionPlan:
    duration: float  # possibly reduced from the requested value
    offsets: list[float]  # one per join, i.e. len(clip_durations) - 1
    final_duration: float
    reduced: bool  # True if `duration` had to be auto-reduced to fit the clips


def plan_transition(clip_durations: list[float], requested_duration: float) -> TransitionPlan:
    """Computes xfade offsets and validates/auto-reduces the transition duration.

    offset[i] = sum(d[0..i]) - (i+1) * T   for i = 0 .. n-2
    final_duration = sum(d) - (n-1) * T

    Requires T < min(d) / 2; if violated, T is reduced to fit and `reduced`
    is set so the caller can surface a warning.
    """
    if len(clip_durations) < 2:
        raise ValueError("plan_transition requires at least 2 clips")

    max_allowed = min(clip_durations) / 2
    duration = requested_duration
    reduced = False
    if duration >= max_allowed:
        duration = max(0.05, max_allowed * 0.9)
        reduced = True
        logger.warning(
            "Transition duration %.2fs too long for shortest clip; reduced to %.2fs",
            requested_duration, duration,
        )

    offsets = []
    running = 0.0
    for i, d in enumerate(clip_durations[:-1]):
        running += d
        offsets.append(running - (i + 1) * duration)

    final_duration = sum(clip_durations) - (len(clip_durations) - 1) * duration
    return TransitionPlan(duration=duration, offsets=offsets, final_duration=final_duration, reduced=reduced)


def build_xfade_filtergraph(n_clips: int, xfade_name: str, plan: TransitionPlan) -> tuple[str, str, str]:
    """Builds the filter_complex string chaining n_clips video+audio inputs.

    Returns (filter_complex, video_out_label, audio_out_label). Inputs are
    assumed to be ffmpeg input indices 0..n_clips-1, each with a video and
    audio stream (silent audio must already be synthesized upstream for
    clips with no native audio — see app.export.pipeline).
    """
    if n_clips < 2:
        raise ValueError("build_xfade_filtergraph requires at least 2 clips")

    video = video_xfade_chain([f"{i}:v" for i in range(n_clips)], xfade_name, plan.duration, plan.offsets)
    audio = audio_crossfade_chain([f"{i}:a" for i in range(n_clips)], plan.duration)
    # Interleaved per join, as it always was: one string, two chains.
    parts = [part for pair in zip(video, audio, strict=True) for part in pair]
    return ";".join(parts), "vout", "aout"


def video_xfade_chain(
    labels: list[str], xfade_name: str, duration: float, offsets: list[float], digits: int = 3
) -> list[str]:
    """The video half of the join: one xfade per pair, ending in [vout].

    `digits` is how precisely each offset is written. A whole join keeps the
    3 it always had; a batch of one (see app.export.pipeline) writes its
    offsets relative to where the batch starts and needs more, so that each
    transition still starts on the very frame it would in a single pass.
    """
    parts: list[str] = []
    current = labels[0]
    for i in range(1, len(labels)):
        out = f"v{i}" if i < len(labels) - 1 else "vout"
        parts.append(
            f"[{current}][{labels[i]}]xfade=transition={xfade_name}:duration={duration:.3f}:"
            f"offset={offsets[i - 1]:.{digits}f}[{out}]"
        )
        current = out
    return parts


def audio_crossfade_chain(labels: list[str], duration: float) -> list[str]:
    """The audio half of the join: one acrossfade per pair, ending in [aout]."""
    parts: list[str] = []
    current = labels[0]
    for i in range(1, len(labels)):
        out = f"a{i}" if i < len(labels) - 1 else "aout"
        parts.append(f"[{current}][{labels[i]}]acrossfade=d={duration:.3f}:c1=tri:c2=tri[{out}]")
        current = out
    return parts
