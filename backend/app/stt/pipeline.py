"""End-to-end transcription: (optional vocal separation) -> loudness
normalization -> VAD -> voice-only chunks -> STT provider -> merged Transcript.

Two things changed from the desktop build.

**Vocal separation is off by default.** Demucs is torch, and torch does not
read the container's cgroup CPU limit: it sizes its thread pool from the
HOST's core count and promptly exceeds the quota this container was granted.
The kernel's response is to throttle every thread in the container — including
the uvicorn answering the platform health check, which then fails, and the
service is restarted mid-job. It is still available (ENABLE_SEPARATION=1 on a
dedicated worker service) because it genuinely improves accuracy on music-
heavy audio; it is simply no longer something that switches itself on.

**The provider is an interface, not a name.** Everything below talks to
`SttProvider`, so which vendor answers is a construction detail.

Every stage degrades rather than fails: separation, VAD and normalization can
each fall back to the provider's own pause-based chunking. Losing the whole
transcription because one optional stage was unavailable is never the right
answer.
"""
from __future__ import annotations

import importlib.util
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.audio.normalize import normalize_if_too_quiet
from app.audio.vad import VadError, detect_speech_segments
from app.audio.vad_chunking import (
    VoiceExtractionError,
    chunk_text_to_transcript,
    extract_voice_only_wav,
    group_vad_segments_into_chunks,
)
from app.config import Settings
from app.models import Transcript
from app.stt.chunking import merge_transcripts
from app.utils.logging import get_logger
from app.video.audio import extract_audio_16k_mono_wav

logger = get_logger(__name__)

ProgressCallback = Callable[[float], Awaitable[None]]


def torch_available() -> bool:
    """torch/torchaudio are needed by VAD's audio slicing and by Demucs."""
    return all(importlib.util.find_spec(name) is not None for name in ("torch", "torchaudio"))


def vad_available() -> bool:
    return torch_available() and importlib.util.find_spec("silero_vad") is not None


def separation_available(settings: Settings) -> bool:
    return (
        settings.enable_separation
        and torch_available()
        and importlib.util.find_spec("demucs") is not None
    )


async def _provider_chunking(
    client,
    audio_path: Path,
    workdir: Path,
    on_progress: ProgressCallback | None,
    ffmpeg_path: Path,
) -> Transcript:
    """Fallback: hand the whole file to the provider's own pause-based
    chunking.

    `audio_path` is the native-quality extraction — whatever sample rate and
    channel layout the source had — so it is brought to the 16 kHz mono the
    provider's contract names before anything is sent.

    That conversion used to sit behind `torch_available()`, because the
    resampler it called was a torchaudio one living in the separation module.
    torch is deliberately not installed here, so the branch never ran and the
    provider was handed 48 kHz stereo: at 192 kB/s a 59s chunk is 11 MB, and
    the API answered chunks that size with 500s and bodiless 520s. Resampling
    is an ffmpeg one-liner and ffmpeg is a hard requirement of this image, so
    there is nothing for the conversion to be conditional on.

    `on_progress` is nudged immediately: a caller watching a percentage would
    otherwise see it frozen wherever the pipeline gave up, making a fallback
    that is running look stuck.
    """
    target = audio_path
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        converted = workdir / "stt_16k_mono.wav"
        await extract_audio_16k_mono_wav(ffmpeg_path, audio_path, converted)
        target = converted
    except Exception:  # noqa: BLE001 - best effort; the original still transcribes
        logger.exception("Conversion to 16kHz mono failed; sending the original audio as-is")
    if on_progress:
        await on_progress(0.5)
    return await client.transcribe(target)


async def transcribe_audio(
    client,
    audio_path: Path,
    workdir: Path,
    settings: Settings,
    ffmpeg_path: Path,
    on_progress: ProgressCallback | None = None,
) -> Transcript:
    """Transcribe `audio_path` (read only, never modified).

    `workdir` is scratch space for this job: on a container it is deleted
    when the job ends, so nothing here may be the only copy of anything.
    Artifacts worth keeping are uploaded to R2 by the caller.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    speech_path = audio_path

    if separation_available(settings):
        from app.audio.separation import SeparationError, separate_vocals

        vocals_path = workdir / "vocals.wav"
        music_path = workdir / "music.wav"
        try:
            await separate_vocals(
                audio_path,
                vocals_path,
                music_path,
                settings.resolved_model_cache_dir
                if hasattr(settings, "resolved_model_cache_dir")
                else workdir / "models",
                settings.demucs_model,
            )
            speech_path = vocals_path
        except SeparationError:
            logger.exception("Vocal separation failed; continuing on the unseparated audio")
    elif settings.enable_separation:
        logger.warning("ENABLE_SEPARATION is set but demucs/torch are not installed; skipping separation")

    if on_progress:
        await on_progress(0.3)

    if not vad_available():
        logger.info("Silero VAD unavailable; using the provider's pause-based chunking")
        return await _provider_chunking(client, audio_path, workdir, on_progress, ffmpeg_path)

    # Only replaces the file when the audio really was too quiet; loud-enough
    # speech passes through untouched.
    speech_path = await normalize_if_too_quiet(ffmpeg_path, speech_path, workdir / "normalized.wav")

    try:
        vad_segments = await detect_speech_segments(
            speech_path,
            threshold=settings.vad_threshold,
            min_speech_ms=settings.vad_min_speech_ms,
            min_silence_ms=settings.vad_min_silence_ms,
            speech_pad_ms=settings.vad_speech_pad_ms,
        )
    except VadError:
        logger.exception("VAD failed; falling back to the provider's pause-based chunking")
        return await _provider_chunking(client, audio_path, workdir, on_progress, ffmpeg_path)

    if on_progress:
        await on_progress(0.4)

    (workdir / "vad_segments.json").write_text(
        json.dumps([{"start": s, "end": e} for s, e in vad_segments], indent=2), encoding="utf-8"
    )

    if not vad_segments:
        logger.info("VAD found no speech in %s", audio_path)
        return Transcript(language="", segments=[], full_text="", timings_estimated=False)

    chunk_groups = group_vad_segments_into_chunks(vad_segments, max_chunk_sec=client.max_audio_sec)
    logger.info("Grouped %d VAD segment(s) into %d request chunk(s)", len(vad_segments), len(chunk_groups))

    chunks_dir = workdir / "voice_chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    chunk_transcripts: list[Transcript] = []
    for i, group in enumerate(chunk_groups):
        chunk_path = chunks_dir / f"chunk_{i:03d}.wav"
        try:
            await extract_voice_only_wav(speech_path, group, chunk_path)
        except VoiceExtractionError:
            logger.exception("Voice-only extraction failed for chunk %d; skipping it", i)
            continue
        text = await client.transcribe_chunk_text(chunk_path)
        chunk_transcripts.append(chunk_text_to_transcript(text, group))
        if on_progress:
            await on_progress(0.4 + 0.6 * (i + 1) / len(chunk_groups))

    return merge_transcripts([(0.0, t) for t in chunk_transcripts])
