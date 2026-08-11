from app.video.probe import _parse_fps


def test_parse_fps_fraction():
    assert _parse_fps("30000/1001") == 30000 / 1001


def test_parse_fps_whole_fraction():
    assert _parse_fps("25/1") == 25.0


def test_parse_fps_zero_over_zero():
    assert _parse_fps("0/0") == 0.0


def test_parse_fps_empty():
    assert _parse_fps("") == 0.0


def test_parse_fps_plain_number():
    assert _parse_fps("30") == 30.0
