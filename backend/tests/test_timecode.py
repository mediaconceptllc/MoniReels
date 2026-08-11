from app.utils.timecode import (
    ass_to_seconds,
    clock_to_seconds,
    ffmpeg_out_time_to_seconds,
    seconds_to_ass,
    seconds_to_clock,
    seconds_to_mmss,
    seconds_to_srt,
    srt_to_seconds,
)


def test_seconds_to_srt_basic():
    assert seconds_to_srt(0) == "00:00:00,000"
    assert seconds_to_srt(65.5) == "00:01:05,500"
    assert seconds_to_srt(3661.001) == "01:01:01,001"


def test_srt_roundtrip():
    for value in [0.0, 1.234, 59.999, 3600.5, 7325.001]:
        assert abs(srt_to_seconds(seconds_to_srt(value)) - value) < 0.001


def test_seconds_to_ass_basic():
    assert seconds_to_ass(0) == "0:00:00.00"
    assert seconds_to_ass(65.5) == "0:01:05.50"
    assert seconds_to_ass(3661.01) == "1:01:01.01"


def test_ass_roundtrip():
    for value in [0.0, 1.23, 59.99, 3600.5]:
        assert abs(ass_to_seconds(seconds_to_ass(value)) - value) < 0.01


def test_clock_roundtrip():
    for value in [0.0, 1.234, 3661.001]:
        assert abs(clock_to_seconds(seconds_to_clock(value)) - value) < 0.001


def test_seconds_to_mmss():
    assert seconds_to_mmss(0) == "00:00"
    assert seconds_to_mmss(90) == "01:30"
    assert seconds_to_mmss(3599) == "59:59"


def test_ffmpeg_out_time_to_seconds():
    assert ffmpeg_out_time_to_seconds("00:00:10.500000") == 10.5
    assert ffmpeg_out_time_to_seconds("01:00:00.000000") == 3600.0
    assert ffmpeg_out_time_to_seconds("N/A") == 0.0
