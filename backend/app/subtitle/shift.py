"""Re-times transcript segments after cutting: maps segments from source-video
time onto the final rendered timeline built from a list of clips. Segments
outside every kept clip are dropped; segments straddling a clip boundary are
split at the boundary (using word-level timings when available).
"""
from __future__ import annotations

import uuid

from app.models import Segment, Word
from app.timeline.models import Clip


def _split_segment_for_clip(segment: Segment, clip: Clip, output_offset: float) -> Segment | None:
    overlap_start = max(segment.start, clip.start)
    overlap_end = min(segment.end, clip.end)
    if overlap_end <= overlap_start:
        return None

    def to_output(t: float) -> float:
        return t - clip.start + output_offset

    if segment.words:
        kept = [w for w in segment.words if w.start < overlap_end and w.end > overlap_start]
        if not kept:
            return None
        text = " ".join(w.text for w in kept)
        seg_start = max(overlap_start, kept[0].start)
        seg_end = min(overlap_end, kept[-1].end)
        shifted_words = [Word(start=to_output(w.start), end=to_output(w.end), text=w.text) for w in kept]
    else:
        text = segment.text
        seg_start, seg_end = overlap_start, overlap_end
        shifted_words = []

    return Segment(
        id=uuid.uuid4().hex,
        start=to_output(seg_start),
        end=to_output(seg_end),
        text=text,
        speaker=segment.speaker,
        words=shifted_words,
    )


def retime_segments_for_output(
    segments: list[Segment],
    clips: list[Clip],
    clip_output_starts: list[float],
    transcript_source: str | None = None,
) -> list[Segment]:
    """clip_output_starts[i] is where clips[i]'s content begins in the final
    rendered output — cumulative durations for a straight concat join, or the
    xfade offsets (see app.transition.filtergraph.plan_transition) when a
    crossfade is used, so subtitle timing stays in sync with either join type.

    Positional: clip_output_starts[i] must correspond to clips[i] exactly as
    passed in (i.e. already in render/timeline order) — this function does not
    itself sort clips, it only sorts the resulting segments by their new start.

    `transcript_source` names the file the segments were transcribed FROM.
    A timeline can hold clips cut from somewhere else — a brand intro or
    outro — and those overlap the transcript's timeline by coincidence of
    seconds, not because anyone said those words: an intro running 0-6s would
    otherwise be captioned with whatever the source video says in its first
    six. Left None, every clip is treated as the source's, which is what a
    timeline of nothing but cuts always was.
    """
    result: list[Segment] = []
    for clip, output_start in zip(clips, clip_output_starts, strict=True):
        if transcript_source is not None and clip.source_path != transcript_source:
            continue
        for segment in segments:
            shifted = _split_segment_for_clip(segment, clip, output_start)
            if shifted is not None:
                result.append(shifted)

    result.sort(key=lambda s: s.start)
    return result
