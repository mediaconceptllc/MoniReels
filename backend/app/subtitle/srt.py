"""SRT generation. Always written UTF-8 without BOM (Python's plain "utf-8"
codec never emits one — only "utf-8-sig" does)."""
from __future__ import annotations

from app.models import Segment
from app.subtitle.cues import to_cues
from app.utils.timecode import seconds_to_srt


def segments_to_srt(segments: list[Segment]) -> str:
    """Transcript segments in, subtitle file out.

    The split to cue-sized blocks happens HERE, not in the callers: this and
    build_ass_document are every subtitle this system emits, and a caller
    that forgot would ship a file whose text sits on screen for half a minute
    with nothing to say it went wrong.
    """
    blocks = []
    for i, seg in enumerate(sorted(to_cues(segments), key=lambda s: s.start), start=1):
        blocks.append(f"{i}\n{seconds_to_srt(seg.start)} --> {seconds_to_srt(seg.end)}\n{seg.text}\n")
    return "\n".join(blocks)
