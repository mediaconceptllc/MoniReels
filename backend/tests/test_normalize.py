"""app.audio.normalize tests. Real ffmpeg subprocess calls are monkeypatched
at the private-function boundary (same convention as the rest of this
suite - see test_separation.py, test_vad_chunking.py) so this stays
hermetic; the real loudnorm measurement + ffmpeg-normalize correction was
verified ad hoc against a real quiet test tone during development.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import app.audio.normalize as normalize_mod
from app.audio.normalize import TARGET_LUFS, TOO_QUIET_MARGIN_DB, normalize_if_too_quiet


@pytest.mark.asyncio
async def test_skips_normalization_when_already_loud_enough(tmp_path, monkeypatch):
    async def fake_measure(ffmpeg_path, wav_path):
        return TARGET_LUFS  # exactly at target - no boost needed

    def fail_if_called(*args, **kwargs):
        raise AssertionError("_normalize_sync should not be called when already loud enough")

    monkeypatch.setattr(normalize_mod, "_measure_integrated_loudness", fake_measure)
    monkeypatch.setattr(normalize_mod, "_normalize_sync", fail_if_called)

    wav_path = tmp_path / "vocals.wav"
    out_path = tmp_path / "vocals_normalized.wav"
    result = await normalize_if_too_quiet(Path("ffmpeg"), wav_path, out_path)

    assert result == wav_path
    assert not out_path.exists()


@pytest.mark.asyncio
async def test_skips_normalization_within_margin_of_target(tmp_path, monkeypatch):
    async def fake_measure(ffmpeg_path, wav_path):
        return TARGET_LUFS - (TOO_QUIET_MARGIN_DB - 0.1)  # just inside the margin

    monkeypatch.setattr(normalize_mod, "_measure_integrated_loudness", fake_measure)
    monkeypatch.setattr(
        normalize_mod, "_normalize_sync", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called"))
    )

    result = await normalize_if_too_quiet(Path("ffmpeg"), tmp_path / "vocals.wav", tmp_path / "out.wav")

    assert result == tmp_path / "vocals.wav"


@pytest.mark.asyncio
async def test_normalizes_when_too_quiet(tmp_path, monkeypatch):
    calls = {}

    async def fake_measure(ffmpeg_path, wav_path):
        return -40.0  # well below target

    def fake_normalize_sync(ffmpeg_path, in_path, out_path, target_level):
        calls["args"] = (ffmpeg_path, in_path, out_path, target_level)
        out_path.write_bytes(b"normalized")

    monkeypatch.setattr(normalize_mod, "_measure_integrated_loudness", fake_measure)
    monkeypatch.setattr(normalize_mod, "_normalize_sync", fake_normalize_sync)

    wav_path = tmp_path / "vocals.wav"
    out_path = tmp_path / "vocals_normalized.wav"
    ffmpeg_path = Path("ffmpeg")
    result = await normalize_if_too_quiet(ffmpeg_path, wav_path, out_path)

    assert result == out_path
    assert out_path.read_bytes() == b"normalized"
    assert calls["args"] == (ffmpeg_path, wav_path, out_path, TARGET_LUFS)


@pytest.mark.asyncio
async def test_falls_back_to_original_when_measurement_fails(tmp_path, monkeypatch):
    async def fake_measure(ffmpeg_path, wav_path):
        raise normalize_mod.NormalizeError("could not parse ffmpeg output")

    monkeypatch.setattr(normalize_mod, "_measure_integrated_loudness", fake_measure)

    wav_path = tmp_path / "vocals.wav"
    result = await normalize_if_too_quiet(Path("ffmpeg"), wav_path, tmp_path / "out.wav")

    assert result == wav_path


@pytest.mark.asyncio
async def test_falls_back_to_original_when_normalize_itself_fails(tmp_path, monkeypatch):
    async def fake_measure(ffmpeg_path, wav_path):
        return -40.0

    def fake_normalize_sync(*args, **kwargs):
        raise RuntimeError("ffmpeg-normalize blew up")

    monkeypatch.setattr(normalize_mod, "_measure_integrated_loudness", fake_measure)
    monkeypatch.setattr(normalize_mod, "_normalize_sync", fake_normalize_sync)

    wav_path = tmp_path / "vocals.wav"
    result = await normalize_if_too_quiet(Path("ffmpeg"), wav_path, tmp_path / "out.wav")

    assert result == wav_path


def test_measure_integrated_loudness_parses_real_loudnorm_json_shape():
    """Sanity-checks the regex/JSON parsing against loudnorm's real output
    shape (captured from an actual `ffmpeg -af loudnorm=...:print_format=json`
    run) without needing a real subprocess."""
    sample_stderr = (
        "[Parsed_loudnorm_0 @ 0x1] \n"
        "{\n"
        '\t"input_i" : "-55.95",\n'
        '\t"input_tp" : "-30.10",\n'
        '\t"input_lra" : "0.00",\n'
        '\t"input_thresh" : "-66.03",\n'
        '\t"output_i" : "-16.05",\n'
        '\t"target_offset" : "0.05"\n'
        "}\n"
    )
    match = normalize_mod._LOUDNORM_JSON_RE.search(sample_stderr)
    assert match is not None
    import json

    parsed = json.loads(match.group(0))
    assert float(parsed["input_i"]) == pytest.approx(-55.95)
