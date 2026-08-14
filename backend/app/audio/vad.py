"""Voice activity detection via Silero VAD — finds the exact speech-segment
timestamps on the Demucs-isolated vocal stem. These timestamps become the
real subtitle timing downstream (see app.audio.vad_chunking), not a
proportional estimate the way pause-based chunking's timing was.

Unlike Demucs, Silero VAD's weights ship inside the `silero-vad` PyPI
package itself (silero_vad/data/*.jit|*.onnx) - nothing is downloaded at
runtime for this step.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.utils.logging import get_logger

logger = get_logger(__name__)

# Silero VAD only supports 8000 or 16000 Hz input - must match the mono
# 16kHz stem app.audio.separation.separate_vocals produces.
VAD_SAMPLE_RATE = 16000


class VadError(Exception):
    pass


def _detect_speech_segments_sync(
    wav_path: Path,
    threshold: float,
    min_speech_ms: int,
    min_silence_ms: int,
    speech_pad_ms: int,
) -> list[tuple[float, float]]:
    import silero_vad

    model = silero_vad.load_silero_vad()
    audio = silero_vad.read_audio(str(wav_path), sampling_rate=VAD_SAMPLE_RATE)
    timestamps = silero_vad.get_speech_timestamps(
        audio,
        model,
        sampling_rate=VAD_SAMPLE_RATE,
        threshold=threshold,
        min_speech_duration_ms=min_speech_ms,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=speech_pad_ms,
        return_seconds=True,
    )
    return [(float(seg["start"]), float(seg["end"])) for seg in timestamps]


async def detect_speech_segments(
    wav_path: Path,
    threshold: float = 0.5,
    min_speech_ms: int = 250,
    min_silence_ms: int = 150,
    speech_pad_ms: int = 100,
) -> list[tuple[float, float]]:
    """Returns speech intervals (start, end) in seconds, in ascending order,
    detected on `wav_path` - which must be 16kHz mono (see VAD_SAMPLE_RATE).
    """
    logger.info("Running VAD on %s", wav_path)
    try:
        segments = await asyncio.to_thread(
            _detect_speech_segments_sync, wav_path, threshold, min_speech_ms, min_silence_ms, speech_pad_ms
        )
    except Exception as e:  # noqa: BLE001 - surfaced to the caller as a typed error
        raise VadError(f"VAD failed for {wav_path}: {e}") from e
    logger.info("VAD found %d speech segment(s) in %s", len(segments), wav_path)
    return segments
