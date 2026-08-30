"""app.audio.separation tests. Running real Demucs requires downloading the
htdemucs weights (~80MB+) on first use, which is unsuitable for a unit test
suite - the synchronous inference call is monkeypatched instead, mirroring
how test_duudlaga_client.py patches real ffmpeg calls to stay hermetic.
"""
from __future__ import annotations

import pytest

import app.audio.separation as separation
from app.audio.separation import OUTPUT_CHANNELS, OUTPUT_SAMPLE_RATE, SeparationError, separate_vocals


@pytest.mark.asyncio
async def test_separate_vocals_writes_output_and_returns_its_path(tmp_path, monkeypatch):
    calls = {}

    def fake_sync(audio_path, vocals_out_path, music_out_path, model_cache_dir, model_name):
        calls["args"] = (audio_path, vocals_out_path, music_out_path, model_cache_dir, model_name)
        vocals_out_path.write_bytes(b"fake-vocals-wav")
        music_out_path.write_bytes(b"fake-music-wav")

    monkeypatch.setattr(separation, "_separate_vocals_sync", fake_sync)

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"fake-input")
    vocals_out = tmp_path / "vocals.wav"
    music_out = tmp_path / "music.wav"
    model_cache_dir = tmp_path / "models"

    result = await separate_vocals(audio_path, vocals_out, music_out, model_cache_dir, model_name="htdemucs")

    assert result == (vocals_out, music_out)
    assert vocals_out.read_bytes() == b"fake-vocals-wav"
    assert music_out.read_bytes() == b"fake-music-wav"
    assert calls["args"] == (audio_path, vocals_out, music_out, model_cache_dir, "htdemucs")


@pytest.mark.asyncio
async def test_separate_vocals_wraps_failure_in_separation_error(tmp_path, monkeypatch):
    def boom(audio_path, vocals_out_path, music_out_path, model_cache_dir, model_name):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(separation, "_separate_vocals_sync", boom)

    with pytest.raises(SeparationError, match="model download failed"):
        await separate_vocals(
            tmp_path / "in.wav", tmp_path / "vocals.wav", tmp_path / "music.wav", tmp_path / "models"
        )


def test_output_contract_matches_vad_and_stt():
    # Both app.stt.chunking and app.audio.vad assume 16kHz mono.
    assert OUTPUT_SAMPLE_RATE == 16000
    assert OUTPUT_CHANNELS == 1
