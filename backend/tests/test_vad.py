"""app.audio.vad tests. Silero VAD ships its model weights inside the
`silero-vad` package itself (no network download), so this runs a real,
small, offline VAD pass rather than mocking the model - skipped if the
optional ML dependency isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("silero_vad")
pytest.importorskip("torch")
pytest.importorskip("torchaudio")

from app.audio.vad import VAD_SAMPLE_RATE, VadError, detect_speech_segments  # noqa: E402


def _write_silence_wav(path, duration_sec: float):
    import torch
    import torchaudio

    wav = torch.zeros(1, int(duration_sec * VAD_SAMPLE_RATE))
    torchaudio.save(str(path), wav, VAD_SAMPLE_RATE)


def _write_tone_wav(path, duration_sec: float, freq: float = 220.0):
    import math

    import torch
    import torchaudio

    n = int(duration_sec * VAD_SAMPLE_RATE)
    t = torch.arange(n, dtype=torch.float32) / VAD_SAMPLE_RATE
    wav = 0.5 * torch.sin(2 * math.pi * freq * t).unsqueeze(0)
    torchaudio.save(str(path), wav, VAD_SAMPLE_RATE)


@pytest.mark.asyncio
async def test_detect_speech_segments_pure_silence_returns_empty(tmp_path):
    path = tmp_path / "silence.wav"
    _write_silence_wav(path, duration_sec=3.0)

    segments = await detect_speech_segments(path)

    assert segments == []


@pytest.mark.asyncio
async def test_detect_speech_segments_returns_ordered_tuples_or_empty(tmp_path):
    """A pure sine tone isn't real speech, so Silero VAD may reasonably
    return nothing - this just asserts the return shape/ordering contract
    holds (list of ascending, non-overlapping (start, end) tuples) whatever
    it decides, without depending on VAD's actual classification of a tone.
    """
    path = tmp_path / "tone.wav"
    _write_tone_wav(path, duration_sec=2.0)

    segments = await detect_speech_segments(path)

    assert isinstance(segments, list)
    for start, end in segments:
        assert 0.0 <= start < end
    for (_, e1), (s2, _) in zip(segments, segments[1:], strict=False):
        assert s2 >= e1


@pytest.mark.asyncio
async def test_detect_speech_segments_wraps_failure_in_vad_error(tmp_path, monkeypatch):
    import app.audio.vad as vad_mod

    def boom(*args, **kwargs):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(vad_mod, "_detect_speech_segments_sync", boom)

    with pytest.raises(VadError):
        await detect_speech_segments(tmp_path / "missing.wav")
