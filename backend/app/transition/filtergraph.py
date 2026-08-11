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

    parts: list[str] = []
    v_label = "0:v"
    a_label = "0:a"
    for i in range(1, n_clips):
        next_v = f"{i}:v"
        next_a = f"{i}:a"
        out_v = f"v{i}" if i < n_clips - 1 else "vout"
        out_a = f"a{i}" if i < n_clips - 1 else "aout"
        offset = plan.offsets[i - 1]
        parts.append(
            f"[{v_label}][{next_v}]xfade=transition={xfade_name}:duration={plan.duration:.3f}:"
            f"offset={offset:.3f}[{out_v}]"
        )
        parts.append(f"[{a_label}][{next_a}]acrossfade=d={plan.duration:.3f}:c1=tri:c2=tri[{out_a}]")
        v_label, a_label = out_v, out_a

    return ";".join(parts), "vout", "aout"
