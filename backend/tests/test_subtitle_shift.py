from app.models import Segment, Word
from app.subtitle.shift import retime_segments_for_output
from app.timeline.models import Clip


def _clip(order: int, start: float, end: float, source: str = "video.mp4") -> Clip:
    return Clip(id=f"c{order}", source_path=source, start=start, end=end, order=order)


def _segment(
    start: float, end: float, text: str, words: list[tuple[float, float, str]] | None = None
) -> Segment:
    return Segment(
        id="s", start=start, end=end, text=text,
        words=[Word(start=s, end=e, text=t) for s, e, t in (words or [])],
    )


def test_segment_fully_inside_one_clip():
    clips = [_clip(0, 10.0, 30.0)]
    segments = [_segment(15.0, 18.0, "hello")]
    result = retime_segments_for_output(segments, clips, [0.0])
    assert len(result) == 1
    assert result[0].start == 5.0  # 15 - 10 (clip start) + 0 (output start)
    assert result[0].end == 8.0
    assert result[0].text == "hello"


def test_segment_outside_all_clips_dropped():
    clips = [_clip(0, 10.0, 20.0)]
    segments = [_segment(50.0, 55.0, "far away")]
    result = retime_segments_for_output(segments, clips, [0.0])
    assert result == []


def test_segment_straddles_clip_end_split_without_word_timings():
    # clip is [10, 20), segment is [18, 25) -> only [18, 20) overlaps
    clips = [_clip(0, 10.0, 20.0)]
    segments = [_segment(18.0, 25.0, "straddling text")]
    result = retime_segments_for_output(segments, clips, [0.0])
    assert len(result) == 1
    assert result[0].start == 8.0  # 18 - 10
    assert result[0].end == 10.0  # 20 - 10 (clamped to clip end)


def test_segment_spans_two_clips_produces_two_output_segments():
    # source segment [5, 25) overlaps clip0 [0,10) and clip1 [20,30)
    clips = [_clip(0, 0.0, 10.0), _clip(1, 20.0, 30.0)]
    segments = [_segment(5.0, 25.0, "long segment", [(5.0, 25.0, "long segment")])]
    # clip0 output start = 0, clip1 output start = 10 (straight concat)
    result = retime_segments_for_output(segments, clips, [0.0, 10.0])
    assert len(result) == 2
    starts = sorted(r.start for r in result)
    assert starts[0] == 5.0  # from clip0: 5-0+0
    assert starts[1] == 10.0  # from clip1: 20-20+10


def test_word_level_splitting_keeps_only_overlapping_words():
    clips = [_clip(0, 10.0, 20.0)]
    words = [(8.0, 9.0, "before"), (12.0, 13.0, "inside1"), (14.0, 15.0, "inside2"), (21.0, 22.0, "after")]
    segments = [_segment(8.0, 22.0, "before inside1 inside2 after", words)]
    result = retime_segments_for_output(segments, clips, [100.0])
    assert len(result) == 1
    assert result[0].text == "inside1 inside2"
    assert len(result[0].words) == 2
    assert result[0].words[0].start == 102.0  # 12 - 10 + 100
    assert result[0].words[1].end == 105.0  # 15 - 10 + 100


def test_output_shifted_by_xfade_offsets_not_just_concat():
    # Simulates crossfade offsets rather than straight concat sums.
    clips = [_clip(0, 0.0, 10.0), _clip(1, 0.0, 8.0, source="clip1.mp4")]
    segments = [_segment(2.0, 4.0, "hi")]
    result = retime_segments_for_output(segments, clips, [0.0, 9.5])  # xfade offset, not 10.0
    assert result[0].start == 2.0


def test_results_sorted_by_start_regardless_of_input_order():
    # clip_output_starts is positional to `clips` as passed: clips[1] (order=1,
    # the second clip in the timeline) starts at output time 10.0.
    clips = [_clip(1, 20.0, 30.0), _clip(0, 0.0, 10.0)]
    segments = [_segment(25.0, 26.0, "second"), _segment(1.0, 2.0, "first")]
    result = retime_segments_for_output(segments, clips, [10.0, 0.0])
    assert [r.text for r in result] == ["first", "second"]


def test_empty_segments_returns_empty():
    clips = [_clip(0, 0.0, 10.0)]
    assert retime_segments_for_output([], clips, [0.0]) == []
