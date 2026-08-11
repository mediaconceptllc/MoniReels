"""Builds a timeline's Clip list from a set of (start, end) ranges — e.g. a
selected ShortIdea, or a YoutubePlan's keep-ranges."""
from __future__ import annotations

import uuid

from app.timeline.models import Clip


def build_clips_from_ranges(source_path: str, ranges: list[tuple[float, float]]) -> list[Clip]:
    clips = []
    for order, (start, end) in enumerate(ranges):
        if end <= start:
            raise ValueError(f"Range {order} has zero/negative length: [{start}, {end}]")
        clips.append(Clip(id=uuid.uuid4().hex, source_path=source_path, start=start, end=end, order=order))
    return clips
