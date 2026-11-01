"""`import_video`: probe an uploaded source, keep its speech track, make its
thumbnail."""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.db import session_scope
from app.handlers.common import _project_id, _require_ffmpeg
from app.jobs.queue import JobHandle
from app.models import VideoMeta
from app.store import get_row, load, save
from app.utils.logging import get_logger
from app.utils.paths import job_workdir
from app.video.audio import extract_audio_16k_mono_wav
from app.video.probe import generate_thumbnail, probe_video

logger = get_logger(__name__)


async def handle_import_video(handle: JobHandle) -> dict:
    """Probe an uploaded source and make its thumbnail.

    Runs after the browser's direct-to-R2 upload finishes; the API only ever
    recorded the key.
    """
    binaries = _require_ffmpeg()
    workdir = job_workdir(handle.job_id)

    with session_scope() as db:
        row = get_row(db, _project_id(handle))
        source_key = row.video_key
        project_name = row.name
    if not source_key:
        raise RuntimeError("This project has no uploaded source video")

    from app import r2

    await handle.set_progress(0.05, stage="download", message="Fetching the uploaded video")
    local = workdir / f"source{Path(source_key).suffix or '.mp4'}"
    await asyncio.to_thread(r2.download_file, source_key, local)

    await handle.set_progress(0.5, stage="probe", message="Reading video metadata")
    raw = await probe_video(binaries.ffprobe, local)

    # Extract the speech track once, here, while the video is already on
    # disk. Every transcribe used to pull the whole source down again — a
    # 200 MB+ download and a second ffmpeg pass — to arrive at ~32 MB of
    # audio that never changes. Re-runs are what made that hurt: the video is
    # fetched once, the audio is re-read on every attempt.
    audio_key = ""
    if raw["has_audio"]:
        await handle.set_progress(0.6, stage="audio", message="Extracting the audio track")
        audio_local = workdir / "audio.wav"
        await extract_audio_16k_mono_wav(binaries.ffmpeg, local, audio_local)
        audio_key = r2.audio_key(handle.project_id or "", "audio.wav")
        await asyncio.to_thread(r2.upload_file, audio_local, audio_key, "audio/wav")

    await handle.set_progress(0.7, stage="thumbnail", message="Extracting a thumbnail")
    thumb = workdir / "thumbnail.jpg"
    # Half-way in, not the first frame: an opening fade or a black slate makes
    # for a thumbnail that identifies nothing.
    await generate_thumbnail(binaries.ffmpeg, local, thumb, at_sec=max(0.0, raw["duration_sec"] / 2))
    thumb_key = r2.thumbnail_key(handle.project_id or "")
    await asyncio.to_thread(r2.upload_file, thumb, thumb_key, "image/jpeg")

    with session_scope() as db:
        project = load(db, _project_id(handle), for_update=True)
        project.video = VideoMeta(
            source_key=source_key,
            duration_sec=raw["duration_sec"],
            width=raw["width"],
            height=raw["height"],
            fps=raw["fps"],
            has_audio=raw["has_audio"],
            codec=raw["codec"],
            thumbnail_key=thumb_key,
            audio_key=audio_key,
        )
        save(db, project)
        row = get_row(db, project.id)
        row.thumbnail_key = thumb_key
        row.audio_key = audio_key or None

    logger.info("Imported %s (%s): %.1fs", project_name, source_key, raw["duration_sec"])
    return {"duration_sec": raw["duration_sec"], "has_audio": raw["has_audio"]}
