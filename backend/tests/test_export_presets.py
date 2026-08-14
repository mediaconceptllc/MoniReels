from app.export.presets import (
    build_audio_encoder_args,
    build_video_encoder_args,
    choose_encoder,
    resolve_dimensions,
)


def test_resolve_dimensions_landscape():
    assert resolve_dimensions("landscape") == (1920, 1080)


def test_resolve_dimensions_portrait():
    assert resolve_dimensions("portrait") == (1080, 1920)


def test_resolve_dimensions_invalid_raises():
    import pytest

    with pytest.raises(ValueError):
        resolve_dimensions("square")


def test_build_video_encoder_args_libx264():
    args = build_video_encoder_args("libx264", crf=20, preset="medium")
    assert args == ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p"]


def test_build_video_encoder_args_nvenc_uses_cq_not_crf():
    args = build_video_encoder_args("h264_nvenc", crf=23, preset="medium")
    assert "-cq" in args
    assert "-crf" not in args
    assert "23" in args


def test_build_video_encoder_args_nvenc_forces_yuv420p():
    """A filtergraph re-encode (xfade/subtitle burn) can upconvert chroma to
    yuv444p without this - nvenc encodes it anyway, producing a "High 4:4:4
    Predictive" stream only permissive decoders (VLC) can play, which reads
    as a broken export in every standard player.
    """
    args = build_video_encoder_args("h264_nvenc", crf=23, preset="medium")
    idx = args.index("-pix_fmt")
    assert args[idx + 1] == "yuv420p"


def test_build_video_encoder_args_nvenc_maps_unknown_preset_to_default():
    args = build_video_encoder_args("h264_nvenc", crf=23, preset="totally-unknown")
    idx = args.index("-preset")
    assert args[idx + 1] == "p4"


def test_build_video_encoder_args_qsv():
    args = build_video_encoder_args("h264_qsv", crf=25, preset="fast")
    assert args == ["-c:v", "h264_qsv", "-global_quality", "25", "-preset", "fast", "-pix_fmt", "yuv420p"]


def test_build_video_encoder_args_amf():
    args = build_video_encoder_args("h264_amf", crf=22, preset="quality")
    assert "-qp_i" in args and "-qp_p" in args


def test_build_audio_encoder_args():
    assert build_audio_encoder_args() == ["-c:a", "aac", "-b:a", "192k"]


def test_choose_encoder_prefers_hwaccel_when_available_and_enabled():
    assert choose_encoder(use_hwaccel=True, working_hwaccel_encoder="h264_nvenc") == "h264_nvenc"


def test_choose_encoder_falls_back_to_libx264_when_disabled():
    assert choose_encoder(use_hwaccel=False, working_hwaccel_encoder="h264_nvenc") == "libx264"


def test_choose_encoder_falls_back_to_libx264_when_no_working_encoder():
    assert choose_encoder(use_hwaccel=True, working_hwaccel_encoder=None) == "libx264"
