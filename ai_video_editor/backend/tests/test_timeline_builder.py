import pytest

from app.timeline.builder import build_clips_from_ranges


def test_build_clips_from_ranges_basic():
    clips = build_clips_from_ranges("video.mp4", [(0.0, 10.0), (20.0, 30.0)])
    assert len(clips) == 2
    assert clips[0].start == 0.0 and clips[0].end == 10.0 and clips[0].order == 0
    assert clips[1].start == 20.0 and clips[1].end == 30.0 and clips[1].order == 1
    assert clips[0].source_path == "video.mp4"
    assert clips[0].id != clips[1].id


def test_build_clips_from_ranges_rejects_zero_length():
    with pytest.raises(ValueError):
        build_clips_from_ranges("video.mp4", [(5.0, 5.0)])


def test_build_clips_from_ranges_rejects_negative_length():
    with pytest.raises(ValueError):
        build_clips_from_ranges("video.mp4", [(10.0, 5.0)])


def test_build_clips_from_ranges_empty():
    assert build_clips_from_ranges("video.mp4", []) == []
