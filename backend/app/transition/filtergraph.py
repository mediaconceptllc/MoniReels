"""xfade/acrossfade filtergraph construction for joining normalized clips.

Every clip going into this must already share identical width, height, fps,
SAR, and pixel format (video) and sample rate/channel layout (audio) — that
normalization happens per-clip in app.export.pipeline before this ever runs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class TransitionPlan:
    duration: float  # possibly reduced from the requested value
    offsets: list[float]  # one per join, i.e. len(clip_durations) - 1
    final_duration: float
    reduced: bool  # True if `duration` had to be auto-reduced to fit the clips


def plan_transition(
    clip_durations: list[float], requested_duration: float, fps: float | None = None
) -> TransitionPlan:
    """Computes xfade offsets and validates/auto-reduces the transition duration.

    offset[i] = sum(d[0..i]) - (i+1) * T   for i = 0 .. n-2
    final_duration = sum(d) - (n-1) * T

    Requires T < min(d) / 2; if violated, T is reduced to fit and `reduced`
    is set so the caller can surface a warning.

    With `fps`, T is a whole number of frames. Clips that are whole frames
    then put every offset on a frame: xfade starts the next clip's picture
    at the first frame at or after its offset, acrossfade its sound at the
    offset itself, and an offset between two frames left the picture up to
    a frame behind the sound at every join.
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

    if fps:
        # Down when reduced, or when rounding up would reach min(d) / 2: the
        # limit is what keeps a clip's two transitions from meeting.
        frames = round(duration * fps)
        if reduced or frames / fps >= max_allowed:
            frames = math.floor(duration * fps)
        duration = max(1, frames) / fps

    offsets = []
    running = 0.0
    for i, d in enumerate(clip_durations[:-1]):
        running += d
        offsets.append(running - (i + 1) * duration)

    final_duration = sum(clip_durations) - (len(clip_durations) - 1) * duration
    return TransitionPlan(duration=duration, offsets=offsets, final_duration=final_duration, reduced=reduced)


def build_xfade_filtergraph(
    n_clips: int,
    xfade_name: str,
    plan: TransitionPlan,
    audio_samples: list[int] | None = None,
    digits: int = 3,
) -> tuple[str, str, str]:
    """Builds the filter_complex string chaining n_clips video+audio inputs.

    Returns (filter_complex, video_out_label, audio_out_label). Inputs are
    assumed to be ffmpeg input indices 0..n_clips-1, each with a video and
    audio stream (silent audio must already be synthesized upstream for
    clips with no native audio — see app.export.pipeline).

    `audio_samples`, when given, is each clip's length in samples, and every
    input is cut to exactly that before it is crossfaded (see exact_audio).
    `digits` is how precisely offsets and durations are written.
    """
    if n_clips < 2:
        raise ValueError("build_xfade_filtergraph requires at least 2 clips")

    trims: list[str] = []
    audio_labels = [f"{i}:a" for i in range(n_clips)]
    if audio_samples is not None:
        trims, audio_labels = exact_audio(audio_labels, audio_samples)
    video = video_xfade_chain(
        [f"{i}:v" for i in range(n_clips)], xfade_name, plan.duration, plan.offsets, digits
    )
    audio = audio_crossfade_chain(audio_labels, plan.duration, digits)
    # Interleaved per join, as it always was: one string, two chains.
    parts = [*trims, *(part for pair in zip(video, audio, strict=True) for part in pair)]
    return ";".join(parts), "vout", "aout"


def exact_audio(labels: list[str], samples: list[int]) -> tuple[list[str], list[str]]:
    """Each audio input cut to exactly its planned number of samples.

    The clips were cut to that length, but AAC stores whole 1024-sample
    frames and ffmpeg decodes the padding of the last one: every clip came
    back up to 21 ms longer than it was cut. acrossfade and concat have no
    offsets to hold them to, so each clip's surplus pushed every clip after
    it later — MEASURED: the 24th clip's sound 0.78 s behind its picture.

    Returns the trim filters and the labels to chain from instead.
    """
    parts = [
        f"[{label}]atrim=end_sample={count},asetpts=PTS-STARTPTS[x{i}]"
        for i, (label, count) in enumerate(zip(labels, samples, strict=True))
    ]
    return parts, [f"x{i}" for i in range(len(labels))]


def video_xfade_chain(
    labels: list[str], xfade_name: str, duration: float, offsets: list[float], digits: int = 3
) -> list[str]:
    """The video half of the join: one xfade per pair, ending in [vout].

    `digits` is how precisely each offset and the duration are written. The
    export writes 6: an offset that lies exactly on a frame, rounded UP to
    the millisecond, lands just after it, and xfade then starts the next
    clip a frame late.
    """
    parts: list[str] = []
    current = labels[0]
    for i in range(1, len(labels)):
        out = f"v{i}" if i < len(labels) - 1 else "vout"
        parts.append(
            f"[{current}][{labels[i]}]xfade=transition={xfade_name}:duration={duration:.{digits}f}:"
            f"offset={offsets[i - 1]:.{digits}f}[{out}]"
        )
        current = out
    return parts


def audio_crossfade_chain(labels: list[str], duration: float, digits: int = 3) -> list[str]:
    """The audio half of the join: one acrossfade per pair, ending in [aout].

    Every overlap is written to `digits` places: a frame-length transition
    at 29.97 is 0.5005005 s, and written as 0.501 each join took half a
    millisecond too much out of the sound."""
    parts: list[str] = []
    current = labels[0]
    for i in range(1, len(labels)):
        out = f"a{i}" if i < len(labels) - 1 else "aout"
        parts.append(
            f"[{current}][{labels[i]}]acrossfade=d={duration:.{digits}f}:c1=tri:c2=tri[{out}]"
        )
        current = out
    return parts
