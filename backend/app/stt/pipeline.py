"""End-to-end transcription pipeline: separate vocals (Demucs) -> normalize
loudness if too quiet -> detect speech (VAD) -> extract voice-only chunks ->
Chimege -> merged Transcript.

The caller's `audio_path` (a native-quality extraction of the source
video's audio, see app.video.audio.extract_audio_native_wav) is only ever
read here, never modified. `workdir` is expected to be the *project*
directory (persistent, not a disposable per-job temp dir) - every derived
artifact is written there so the project keeps a full, inspectable record
of the pipeline: vocals.wav / music.wav (the Demucs split), vocals_normalized.wav
(only written when the vocals were quiet enough to need boosting - see
app.audio.normalize), vad_segments.json (exactly what VAD detected as
speech, in seconds), and voice_chunks/ (the actual gap-free audio sent to
Chimege, one file per chunk).

Falls back to ChimegeClient's own internal silence-based chunking (no
Demucs/VAD at all) when torch/demucs/silero_vad aren't importable, so
transcription still works on installs that skipped the optional ML
dependencies - same "degrade, don't hard-fail" spirit as ffmpeg discovery
in app.video.ffmpeg.
"""
from __future__ import annotations

import importlib.util
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.audio.normalize import normalize_if_too_quiet
from app.audio.separation import SeparationError, resample_to_16k_mono, separate_vocals
from app.audio.vad import VadError, detect_speech_segments
from app.audio.vad_chunking import (
    VoiceExtractionError,
    chunk_text_to_transcript,
    extract_voice_only_wav,
    group_vad_segments_into_chunks,
)
from app.config import Settings
from app.models import Transcript
from app.stt.chimege_client import ChimegeClient, merge_transcripts
from app.utils.logging import get_logger
from app.utils.paths import atomic_write_text

logger = get_logger(__name__)

ProgressCallback = Callable[[float], Awaitable[None]]


def ml_deps_available() -> bool:
    required = ("torch", "torchaudio", "demucs", "silero_vad")
    return all(importlib.util.find_spec(name) is not None for name in required)


async def _fallback_to_legacy_chunking(
    client: ChimegeClient, audio_path: Path, workdir: Path, on_progress: ProgressCallback | None
) -> Transcript:
    """Used whenever the Demucs/VAD pipeline can't run. `audio_path` here is
    the *native-quality* extraction (see app.video.audio.
    extract_audio_native_wav) - not yet in Chimege/VAD's required 16kHz
    mono format, unlike vocals.wav in the happy path - so it's resampled
    first; if even that fails, falls through to the original audio as-is
    rather than losing the transcription entirely.

    Also nudges `on_progress` immediately: without this, a caller watching
    progress (e.g. the job's reported percentage) would otherwise see it
    frozen at wherever the pipeline stalled out before falling back, making
    a fallback that's actually running look stuck.
    """
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        target = await resample_to_16k_mono(audio_path, workdir / "fallback_16k_mono.wav")
    except Exception:  # noqa: BLE001 - best-effort; fall through to the original file
        logger.exception("Fallback resample to 16kHz mono failed; using original audio as-is")
        target = audio_path
    if on_progress:
        await on_progress(0.5)
    return await client.transcribe(target)


async def transcribe_with_voice_separation(
    client: ChimegeClient,
    audio_path: Path,
    workdir: Path,
    settings: Settings,
    ffmpeg_path: Path,
    on_progress: ProgressCallback | None = None,
) -> Transcript:
    """Runs the full Demucs -> VAD -> voice-only-chunk -> Chimege pipeline
    against `audio_path`, or falls back to `client.transcribe()` (the
    original whole-file silence-based chunking) if the ML dependencies
    aren't installed or any pipeline stage fails outright.
    """
    if not ml_deps_available():
        logger.warning("Demucs/Silero VAD not installed; falling back to silence-based chunking")
        return await _fallback_to_legacy_chunking(client, audio_path, workdir, on_progress)

    workdir.mkdir(parents=True, exist_ok=True)

    vocals_path = workdir / "vocals.wav"
    music_path = workdir / "music.wav"
    try:
        await separate_vocals(
            audio_path, vocals_path, music_path, settings.resolved_model_cache_dir, settings.demucs_model
        )
    except SeparationError:
        logger.exception("Vocal separation failed; falling back to silence-based chunking")
        return await _fallback_to_legacy_chunking(client, audio_path, workdir, on_progress)
    if on_progress:
        await on_progress(0.3)

    # Only overwrites `vocals_path` with the normalized version when the
    # audio was actually too quiet - already loud-enough vocals pass
    # through unchanged, and vocals_normalized.wav simply doesn't exist in
    # that case.
    vocals_path = await normalize_if_too_quiet(ffmpeg_path, vocals_path, workdir / "vocals_normalized.wav")

    try:
        vad_segments = await detect_speech_segments(
            vocals_path,
            threshold=settings.vad_threshold,
            min_speech_ms=settings.vad_min_speech_ms,
            min_silence_ms=settings.vad_min_silence_ms,
            speech_pad_ms=settings.vad_speech_pad_ms,
        )
    except VadError:
        logger.exception("VAD failed; falling back to silence-based chunking")
        return await _fallback_to_legacy_chunking(client, audio_path, workdir, on_progress)
    if on_progress:
        await on_progress(0.4)

    atomic_write_text(
        workdir / "vad_segments.json",
        json.dumps([{"start": s, "end": e} for s, e in vad_segments], indent=2),
    )

    if not vad_segments:
        logger.info("VAD found no speech in %s", audio_path)
        return Transcript(language="", segments=[], full_text="", timings_estimated=False)

    chunk_groups = group_vad_segments_into_chunks(vad_segments, max_chunk_sec=client.max_audio_sec)
    logger.info("Grouped %d VAD segment(s) into %d Chimege chunk(s)", len(vad_segments), len(chunk_groups))

    chunks_dir = workdir / "voice_chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    chunk_transcripts: list[Transcript] = []
    for i, group in enumerate(chunk_groups):
        chunk_path = chunks_dir / f"chunk_{i:03d}.wav"
        try:
            await extract_voice_only_wav(vocals_path, group, chunk_path)
        except VoiceExtractionError:
            logger.exception("Voice-only extraction failed for chunk %d; skipping it", i)
            continue
        text = await client.transcribe_chunk_text(chunk_path)
        chunk_transcripts.append(chunk_text_to_transcript(text, group))
        if on_progress:
            await on_progress(0.4 + 0.6 * (i + 1) / len(chunk_groups))

    return merge_transcripts([(0.0, t) for t in chunk_transcripts])
