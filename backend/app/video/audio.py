"""Audio extraction for STT: always 16 kHz mono 16-bit PCM WAV.

That format is the contract shared by Silero VAD and the STT provider (see
app.stt.chunking) — sending anything else means either a resample nobody
asked for or a rejected request.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.utils.logging import get_logger

logger = get_logger(__name__)


class AudioExtractionError(Exception):
    pass


async def extract_audio_16k_mono_wav(ffmpeg_path: Path, video_path: Path, output_path: Path) -> None:
    args = [
        str(ffmpeg_path),
        "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not output_path.exists():
        raise AudioExtractionError(
            f"Audio extraction failed for {video_path}: {stderr.decode(errors='replace')[-500:]}"
        )


async def extract_audio_native_wav(ffmpeg_path: Path, video_path: Path, output_path: Path) -> None:
    """Extracts audio at its native sample rate/channel layout (no forced
    16k/mono downmix) - used as Demucs' separation input (see
    app/audio/separation.py), since source separation quality degrades on
    audio already narrowed to the 16kHz mono STT contract. The 16kHz mono
    16kHz mono contract is applied afterwards, downstream of separation.
    """
    args = [
        str(ffmpeg_path),
        "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vn",
        "-c:a", "pcm_s16le",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not output_path.exists():
        raise AudioExtractionError(
            f"Audio extraction failed for {video_path}: {stderr.decode(errors='replace')[-500:]}"
        )
