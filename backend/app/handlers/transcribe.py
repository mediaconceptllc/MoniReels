"""`transcribe`: the project's speech as a timed transcript, and its SRT."""
from __future__ import annotations

import asyncio
from pathlib import Path

from app import provider_settings
from app.db import session_scope
from app.handlers.common import _project_id, _require_ffmpeg
from app.jobs.queue import JobHandle
from app.models import Transcript
from app.store import load, save
from app.stt import chunk_cache
from app.stt.factory import build_client as build_stt_client
from app.stt.factory import for_language as stt_for_language
from app.stt.pipeline import separation_available, transcribe_audio
from app.subtitle.srt import segments_to_srt
from app.utils.paths import job_workdir
from app.video.audio import extract_audio_16k_mono_wav, extract_audio_native_wav


async def handle_transcribe(handle: JobHandle) -> dict:
    binaries = _require_ffmpeg()
    workdir = job_workdir(handle.job_id)

    from app import r2

    with session_scope() as db:
        # Read per job rather than once at worker start: an admin who fixes
        # a key expects the next job to use it, not the next deploy — and
        # the API process has no way to push the new value to this one.
        settings = provider_settings.effective(db)
        project = load(db, _project_id(handle))
    if project.video is None:
        raise RuntimeError("This project has no video to transcribe yet")
    if not project.video.has_audio:
        raise RuntimeError("This video has no audio track")

    audio_path = workdir / "audio.wav"
    # Demucs is the one consumer that wants the source's own sample rate:
    # separation degrades on audio already narrowed to the 16 kHz mono STT
    # contract, so the stored copy is a shortcut only while separation is off.
    separating = separation_available(settings)
    if project.video.audio_key and not separating:
        # ~32 MB instead of the whole source, and no second ffmpeg pass.
        await handle.set_progress(0.02, stage="download", message="Fetching the audio")
        await asyncio.to_thread(r2.download_file, project.video.audio_key, audio_path)
    else:
        await handle.set_progress(0.02, stage="download", message="Fetching the video")
        local = workdir / f"source{Path(project.video.source_key).suffix or '.mp4'}"
        await asyncio.to_thread(r2.download_file, project.video.source_key, local)

        await handle.set_progress(0.05, stage="extract_audio", message="Extracting audio")
        if separating:
            await extract_audio_native_wav(binaries.ffmpeg, local, audio_path)
        else:
            await extract_audio_16k_mono_wav(binaries.ffmpeg, local, audio_path)

    # Heard in the language it is spoken in — see stt.factory.for_language.
    settings = stt_for_language(settings, project.language)
    client = build_stt_client(settings)
    # A client that sends chunk by chunk reads back what an unfinished run of
    # this project already paid for (app.stt.chunk_cache), and keeps each new
    # answer the moment it arrives.
    cache = None
    variant = getattr(client, "variant", None)
    if variant is not None:
        cache = chunk_cache.ChunkCache(_project_id(handle), variant)
        client.chunk_cache = cache
    try:
        async def on_progress(p: float) -> None:
            await handle.set_progress(0.1 + p * 0.85, stage="transcribing", message="Transcribing speech")

        transcript: Transcript = await transcribe_audio(
            client, audio_path, workdir, settings, binaries.ffmpeg, on_progress=on_progress
        )
    finally:
        await client.aclose()

    handle.raise_if_cancelled()

    srt_key = None
    if transcript.segments:
        srt_path = workdir / "subtitles.srt"
        srt_path.write_text(segments_to_srt(transcript.segments), encoding="utf-8")
        srt_key = r2.audio_key(_project_id(handle), "subtitles.srt")
        await asyncio.to_thread(r2.upload_file, srt_path, srt_key, "text/plain; charset=utf-8")

    with session_scope() as db:
        project = load(db, _project_id(handle), for_update=True)
        project.transcript = transcript
        save(db, project)
        # The transcript holds their text now, and a deliberate re-run must
        # hear the audio afresh rather than be handed this run's answers.
        chunk_cache.clear(db, project.id)

    out = {
        "segments": len(transcript.segments),
        "characters": len(transcript.full_text),
        "timings_estimated": transcript.timings_estimated,
        "srt_key": srt_key,
    }
    # Chunks an earlier, unfinished run had paid for and this one read back.
    if cache is not None and cache.hits:
        out["chunks_reused"] = cache.hits
    return out
