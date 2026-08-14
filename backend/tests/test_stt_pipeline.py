"""app.stt.pipeline orchestration tests. Every pipeline stage
(separate_vocals, detect_speech_segments, extract_voice_only_wav) and the
Chimege network call are monkeypatched/faked so this suite never touches
torch, ffmpeg, or the network - it only checks the wiring: which stage
runs, in what order, how failures fall back, and that a full run produces
correctly-merged absolute timestamps.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.stt.pipeline as pipeline_mod
from app.audio.separation import SeparationError
from app.audio.vad import VadError
from app.config import Settings
from app.models import Transcript
from app.stt.pipeline import transcribe_with_voice_separation


class _FakeChimegeClient:
    """Stands in for ChimegeClient: transcribe_chunk_text returns queued
    canned text per call (in order); transcribe() (the legacy fallback) is
    recorded so tests can assert whether it was used instead.
    """

    def __init__(self, chunk_texts: list[str] | None = None):
        self._chunk_texts = iter(chunk_texts or [])
        self.max_audio_sec = 20.0
        self.fallback_calls: list[Path] = []
        self.chunk_calls: list[Path] = []

    async def transcribe_chunk_text(self, chunk_wav_path: Path) -> str:
        self.chunk_calls.append(chunk_wav_path)
        return next(self._chunk_texts)

    async def transcribe(self, audio_path: Path) -> Transcript:
        self.fallback_calls.append(audio_path)
        return Transcript(language="mn", full_text="fallback", segments=[], timings_estimated=True)


def _settings() -> Settings:
    return Settings(_env_file=None)


async def _run(client, tmp_path, **kwargs):
    return await transcribe_with_voice_separation(
        client, tmp_path / "audio.wav", tmp_path / "work", _settings(), Path("ffmpeg"), **kwargs
    )


async def _fake_normalize(ffmpeg_path, wav_path, out_path, *args, **kwargs):
    """Stands in for normalize_if_too_quiet: real ffmpeg loudnorm can't run
    against the fake wav bytes these tests write - a no-op fake keeps the
    pipeline hermetic, same as every other real stage in this suite.
    """
    return wav_path


@pytest.mark.asyncio
async def test_falls_back_when_ml_deps_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "ml_deps_available", lambda: False)
    client = _FakeChimegeClient()

    result = await _run(client, tmp_path)

    assert result.full_text == "fallback"
    assert client.fallback_calls == [tmp_path / "audio.wav"]
    assert client.chunk_calls == []


@pytest.mark.asyncio
async def test_falls_back_on_separation_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "ml_deps_available", lambda: True)

    async def fake_separate(*args, **kwargs):
        raise SeparationError("boom")

    monkeypatch.setattr(pipeline_mod, "separate_vocals", fake_separate)
    client = _FakeChimegeClient()

    result = await _run(client, tmp_path)

    assert result.full_text == "fallback"


@pytest.mark.asyncio
async def test_falls_back_on_vad_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "ml_deps_available", lambda: True)

    async def fake_separate(audio_path, vocals_out_path, music_out_path, model_cache_dir, model_name):
        vocals_out_path.write_bytes(b"fake")
        music_out_path.write_bytes(b"fake")
        return vocals_out_path, music_out_path

    async def fake_vad(*args, **kwargs):
        raise VadError("boom")

    monkeypatch.setattr(pipeline_mod, "separate_vocals", fake_separate)
    monkeypatch.setattr(pipeline_mod, "normalize_if_too_quiet", _fake_normalize)
    monkeypatch.setattr(pipeline_mod, "detect_speech_segments", fake_vad)
    client = _FakeChimegeClient()

    result = await _run(client, tmp_path)

    assert result.full_text == "fallback"


@pytest.mark.asyncio
async def test_no_speech_detected_returns_empty_transcript_without_calling_chimege(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "ml_deps_available", lambda: True)

    async def fake_separate(audio_path, vocals_out_path, music_out_path, model_cache_dir, model_name):
        vocals_out_path.write_bytes(b"fake")
        music_out_path.write_bytes(b"fake")
        return vocals_out_path, music_out_path

    async def fake_vad(*args, **kwargs):
        return []

    monkeypatch.setattr(pipeline_mod, "separate_vocals", fake_separate)
    monkeypatch.setattr(pipeline_mod, "normalize_if_too_quiet", _fake_normalize)
    monkeypatch.setattr(pipeline_mod, "detect_speech_segments", fake_vad)
    client = _FakeChimegeClient()

    result = await _run(client, tmp_path)

    assert result.segments == []
    assert client.chunk_calls == []


@pytest.mark.asyncio
async def test_full_pipeline_produces_absolute_timestamps_per_chunk(tmp_path, monkeypatch):
    """Two VAD segments far enough apart to become two separate chunks;
    each chunk's returned text must land at its own real segment timing,
    not offset/estimated relative to the other.
    """
    monkeypatch.setattr(pipeline_mod, "ml_deps_available", lambda: True)

    async def fake_separate(audio_path, vocals_out_path, music_out_path, model_cache_dir, model_name):
        vocals_out_path.write_bytes(b"fake")
        music_out_path.write_bytes(b"fake")
        return vocals_out_path, music_out_path

    # Each segment is 6s (>= TARGET_CHUNK_MIN_SEC) and client.max_audio_sec
    # is lowered to 10 so the two can't be merged into one request (6+6>10)
    # - guarantees two separate chunks regardless of the real 43s gap
    # between them (grouping is driven by speech-duration budget, not
    # wall-clock proximity, since the gap is dropped either way).
    vad_segments = [(1.0, 7.0), (50.0, 56.0)]

    async def fake_vad(*args, **kwargs):
        return vad_segments

    extracted_groups = []

    async def fake_extract(wav_path, segments, out_path):
        extracted_groups.append(segments)
        out_path.write_bytes(b"fake-chunk")

    monkeypatch.setattr(pipeline_mod, "separate_vocals", fake_separate)
    monkeypatch.setattr(pipeline_mod, "normalize_if_too_quiet", _fake_normalize)
    monkeypatch.setattr(pipeline_mod, "detect_speech_segments", fake_vad)
    monkeypatch.setattr(pipeline_mod, "extract_voice_only_wav", fake_extract)

    client = _FakeChimegeClient(chunk_texts=["Эхний.", "Хоёр дахь."])
    client.max_audio_sec = 10.0
    progress_values = []

    async def on_progress(p):
        progress_values.append(p)

    result = await _run(client, tmp_path, on_progress=on_progress)

    assert len(client.chunk_calls) == 2
    assert len(extracted_groups) == 2
    assert result.full_text == "Эхний. Хоёр дахь."
    assert result.segments[0].start == 1.0
    assert result.segments[0].end == 7.0
    assert result.segments[1].start == 50.0
    assert result.segments[1].end == 56.0
    assert progress_values[-1] == pytest.approx(1.0)

    # workdir (= the project directory in real use) must keep a record of
    # every stage: the Demucs stems, exactly what VAD detected, and the
    # actual gap-free chunk audio sent to Chimege.
    workdir = tmp_path / "work"
    assert (workdir / "vocals.wav").read_bytes() == b"fake"
    assert (workdir / "music.wav").read_bytes() == b"fake"
    saved_segments = json.loads((workdir / "vad_segments.json").read_text(encoding="utf-8"))
    assert saved_segments == [{"start": 1.0, "end": 7.0}, {"start": 50.0, "end": 56.0}]
    assert sorted(p.name for p in (workdir / "voice_chunks").glob("*.wav")) == [
        "chunk_000.wav",
        "chunk_001.wav",
    ]
