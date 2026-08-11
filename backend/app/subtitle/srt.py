"""SRT generation. Always written UTF-8 without BOM (Python's plain "utf-8"
codec never emits one — only "utf-8-sig" does)."""
from __future__ import annotations

from app.models import Segment
from app.utils.timecode import seconds_to_srt


def segments_to_srt(segments: list[Segment]) -> str:
    blocks = []
    for i, seg in enumerate(sorted(segments, key=lambda s: s.start), start=1):
        blocks.append(f"{i}\n{seconds_to_srt(seg.start)} --> {seconds_to_srt(seg.end)}\n{seg.text}\n")
    return "\n".join(blocks)
