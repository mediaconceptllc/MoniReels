"""Transcript's own invariants — the ones every builder has to satisfy.

Segments arrive from three places (a recogniser's word timings, a
character-proportional estimate, per-chunk results merged with an offset) and
each of them can emit a segment whose start and end are the same instant. A
zero-length segment is not a small cue, it is an unusable one: the SRT writer
renders `00:00:12,340 --> 00:00:12,340`, which no player shows, and the cut
planner is offered a unit worth no seconds.

MEASURED in production: one 9-minute transcript logged `shortest 0.0s` among
its 54 cutting units.

The check lives on the model rather than in each builder, so these tests aim
at the model and then confirm the real paths inherit it.
"""
from __future__ import annotations

from app.models import MIN_SEGMENT_SEC, Segment, Transcript
from app.stt.chunking import merge_transcripts
from app.stt.elevenlabs_client import segments_from_words
from app.subtitle.srt import segments_to_srt


def _seg(start: float, end: float, text: str = "хэл") -> Segment:
    return Segment(id="s", start=start, end=end, text=text)


def _tx(segments: list[Segment]) -> Transcript:
    return Transcript(
        language="mn", segments=segments, full_text=" ".join(s.text for s in segments)
    )


def test_zero_length_segment_gets_a_real_interval():
    t = _tx([_seg(12.34, 12.34)])
    assert t.segments[0].end == 12.34 + MIN_SEGMENT_SEC


def test_the_words_are_never_dropped():
    """Somebody said them. Losing transcript to arithmetic is worse than a
    short cue, so the fix lengthens the segment instead of removing it."""
    t = _tx([_seg(1.0, 1.0, "битгий ал")])
    assert len(t.segments) == 1
    assert t.segments[0].text == "битгий ал"


def test_a_zero_length_cue_renders_as_a_visible_srt_interval():
    """The end of the chain, which is where the symptom was seen. worker.py
    writes `segments_to_srt(transcript.segments)`, so the model is what stands
    between a recogniser and an unplayable cue."""
    t = _tx([_seg(12.34, 12.34)])
    assert "00:00:12,340 --> 00:00:12,420" in segments_to_srt(t.segments)


def test_an_end_never_moves_past_the_next_start():
    """Pushing an end forward must not open an overlap or reorder anything."""
    t = _tx([_seg(5.0, 5.0), _seg(5.02, 6.0)])
    assert t.segments[0].end == 5.02
    assert t.segments[0].end <= t.segments[1].start


def test_a_segment_with_no_room_is_left_alone():
    """Reporting it honestly beats inventing time that is not there: the next
    segment starts at the same instant, so there is nowhere to grow."""
    t = _tx([_seg(5.0, 5.0), _seg(5.0, 7.0)])
    assert t.segments[0].start == t.segments[0].end == 5.0


def test_ordinary_segments_are_untouched():
    t = _tx([_seg(0.0, 2.0), _seg(2.0, 4.5)])
    assert [(s.start, s.end) for s in t.segments] == [(0.0, 2.0), (2.0, 4.5)]


def test_word_timings_that_collapse_to_a_point():
    """A recogniser reporting one word's start and end as the same instant is
    the path this was first seen on."""
    words = [{"text": "тийм", "start": 12.34, "end": 12.34, "type": "word"}]
    t = _tx(segments_from_words(words))
    assert t.segments[0].end - t.segments[0].start >= MIN_SEGMENT_SEC


def test_a_packed_run_of_short_segments_never_overlaps():
    """The limit of what this can do, stated rather than hidden.

    Segments packed tighter than the minimum cannot all reach it without
    overlapping, and an overlap is the worse outcome — two cues on screen at
    once, and a cut planner handed the same second twice. Each one still gets
    a real interval; the end simply stops where the next segment starts.
    """
    t = _tx([_seg(0.0, 0.0), _seg(0.02, 0.02), _seg(0.04, 1.0)])
    assert [(s.start, s.end) for s in t.segments] == [(0.0, 0.02), (0.02, 0.04), (0.04, 1.0)]


def test_merged_chunks_keep_the_invariant():
    """The merge path: offsets are added per chunk, and a collapsed segment
    stays collapsed through the addition."""
    a = _tx([_seg(0.0, 1.0)])
    b = _tx([_seg(0.0, 0.0, "яв")])
    merged = merge_transcripts([(0.0, a), (10.0, b)])
    assert all(s.end - s.start >= MIN_SEGMENT_SEC for s in merged.segments)
