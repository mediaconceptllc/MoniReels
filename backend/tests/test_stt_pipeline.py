"""app.stt.pipeline wiring.

Every real stage (Demucs, Silero VAD, loudness normalization, the network
call) is faked, so this suite never touches torch, ffmpeg or the network. It
checks the decisions the pipeline makes: which stages run, how each failure
degrades, and that a full run produces correctly merged absolute timestamps.

The degradation rules are the point. Vocal separation is optional and VAD may
be absent from the image entirely, so "one stage was unavailable" must never
become "the transcription failed".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.stt.pipeline as pipeline_mod
from app.audio.vad import VadError
from app.config import Settings
from app.models import Transcript
from app.stt.pipeline import transcribe_audio


class _FakeSttClient:
    """Stands in for DuudlagaClient.

    `transcribe_chunk_text` returns queued canned text per call; `transcribe`
    (the whole-file fallback) records that it was used, so a test can tell
    which path the pipeline actually took.
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


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


async def _fake_normalize(ffmpeg_path, wav_path, out_path, *args, **kwargs):
    """Real ffmpeg loudnorm cannot run against the placeholder bytes these
    tests write; a pass-through keeps the suite hermetic."""
    return wav_path


async def _run(client, tmp_path, settings=None, **kwargs):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF....WAVE")
    return await transcribe_audio(
        client, audio, tmp_path / "work", settings or _settings(), Path("ffmpeg"), **kwargs
    )


@pytest.fixture(autouse=True)
def _no_real_normalize(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "normalize_if_too_quiet", _fake_normalize)


def _with_vad(monkeypatch, segments, available: bool = True):
    monkeypatch.setattr(pipeline_mod, "vad_available", lambda: available)
    monkeypatch.setattr(pipeline_mod, "torch_available", lambda: True)

    async def _detect(*args, **kwargs):
        return segments

    monkeypatch.setattr(pipeline_mod, "detect_speech_segments", _detect)


@pytest.mark.asyncio
async def test_falls_back_to_provider_chunking_when_vad_missing(tmp_path, monkeypatch):
    """An image built without torch is the DEFAULT deployment, not an edge
    case — transcription must still work there."""
    monkeypatch.setattr(pipeline_mod, "vad_available", lambda: False)
    monkeypatch.setattr(pipeline_mod, "torch_available", lambda: False)
    client = _FakeSttClient()

    result = await _run(client, tmp_path)

    assert result.full_text == "fallback"
    assert len(client.fallback_calls) == 1
    assert client.chunk_calls == []


@pytest.mark.asyncio
async def test_separation_is_off_unless_explicitly_enabled(tmp_path, monkeypatch):
    """Demucs must never switch itself on: on a container that also serves
    HTTP it throttles the whole cgroup and the platform restarts the service
    mid-job."""
    called = []

    async def _separate(*args, **kwargs):
        called.append(args)
        raise AssertionError("separation ran without ENABLE_SEPARATION")

    monkeypatch.setattr("app.audio.separation.separate_vocals", _separate)
    _with_vad(monkeypatch, [])
    await _run(_FakeSttClient(), tmp_path, settings=_settings(enable_separation=False))
    assert called == []


@pytest.mark.asyncio
async def test_falls_back_on_vad_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "vad_available", lambda: True)
    monkeypatch.setattr(pipeline_mod, "torch_available", lambda: True)

    async def _boom(*args, **kwargs):
        raise VadError("no model")

    monkeypatch.setattr(pipeline_mod, "detect_speech_segments", _boom)
    client = _FakeSttClient()

    result = await _run(client, tmp_path)

    assert result.full_text == "fallback"
    assert len(client.fallback_calls) == 1


@pytest.mark.asyncio
async def test_no_speech_returns_empty_transcript_without_spending_money(tmp_path, monkeypatch):
    """Silence must not reach the provider: every request is billed, and a
    request containing no speech can only return nothing."""
    _with_vad(monkeypatch, [])
    client = _FakeSttClient()

    result = await _run(client, tmp_path)

    assert result.segments == []
    assert result.full_text == ""
    assert client.chunk_calls == []
    assert client.fallback_calls == []


@pytest.mark.asyncio
async def test_full_run_produces_absolute_timestamps(tmp_path, monkeypatch):
    """Each chunk is transcribed in its own coordinate space; the merge must
    put every segment back on the timeline of the source video."""
    _with_vad(monkeypatch, [(1.0, 3.0), (10.0, 12.0)])

    async def _extract(wav_path, segments, out_path):
        out_path.write_bytes(b"chunk")

    monkeypatch.setattr(pipeline_mod, "extract_voice_only_wav", _extract)

    # max_audio_sec 20 with 2s of speech per group: each VAD segment becomes
    # its own chunk only if grouping keeps them apart, so assert on the
    # timeline rather than on the chunk count.
    client = _FakeSttClient(["эхний хэсэг", "хоёр дахь хэсэг"])
    result = await _run(client, tmp_path)

    assert result.segments, "expected at least one segment"
    starts = [s.start for s in result.segments]
    assert min(starts) >= 1.0
    assert max(s.end for s in result.segments) <= 12.0
    # VAD output is written out for inspection: when a transcript looks wrong
    # the first question is always "what did VAD actually detect".
    saved = json.loads((tmp_path / "work" / "vad_segments.json").read_text())
    assert saved == [{"start": 1.0, "end": 3.0}, {"start": 10.0, "end": 12.0}]


@pytest.mark.asyncio
async def test_source_audio_is_never_modified(tmp_path, monkeypatch):
    _with_vad(monkeypatch, [])
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF....WAVE")
    before = audio.read_bytes()

    await transcribe_audio(
        _FakeSttClient(), audio, tmp_path / "work", _settings(), Path("ffmpeg")
    )
    assert audio.read_bytes() == before


# ---------------------------------------------------------------------------
# The 16 kHz mono contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_provider_gets_16k_mono_even_though_torch_is_absent(tmp_path, monkeypatch):
    """The conversion used to sit behind torch_available(), because the
    resampler it called was a torchaudio one in the separation module. torch
    is deliberately not in this image, so the branch never ran and the API was
    handed the source's own 48 kHz stereo: 192 kB/s, which made a 59s chunk
    11 MB — and the API answered chunks that size with 500s and 520s."""
    monkeypatch.setattr(pipeline_mod, "vad_available", lambda: False)
    monkeypatch.setattr(pipeline_mod, "torch_available", lambda: False)
    converted: list[tuple[Path, Path]] = []

    async def _convert(ffmpeg_path, src, out):
        converted.append((src, out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"RIFF16k")

    monkeypatch.setattr(pipeline_mod, "extract_audio_16k_mono_wav", _convert)

    client = _FakeSttClient()
    await _run(client, tmp_path)

    assert len(converted) == 1, "the audio was sent without being converted"
    # What reaches the provider is the converted file, not the original.
    assert client.fallback_calls == [converted[0][1]]
    assert client.fallback_calls[0].name == "stt_16k_mono.wav"


@pytest.mark.asyncio
async def test_a_failed_conversion_still_sends_the_audio(tmp_path, monkeypatch):
    """Degrading to the wrong sample rate is worse than nothing only if the
    request fails outright — a transcription that might work still beats a
    job that certainly does not."""
    monkeypatch.setattr(pipeline_mod, "vad_available", lambda: False)

    async def _boom(ffmpeg_path, src, out):
        raise RuntimeError("ffmpeg said no")

    monkeypatch.setattr(pipeline_mod, "extract_audio_16k_mono_wav", _boom)

    client = _FakeSttClient()
    await _run(client, tmp_path)

    assert client.fallback_calls[0].name == "audio.wav"
