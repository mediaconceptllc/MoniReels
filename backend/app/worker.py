"""Worker process. Run as its own Railway service: `python -m app.worker`.

Why a separate process rather than a background task inside the API:

ffmpeg and Demucs saturate the box. When they share a container with uvicorn,
the platform's health check stops being answered in time and the service is
restarted — killing the very job that caused the load, and cutting any R2
upload in flight (which then leaves billed multipart garbage behind). Splitting
them means a render can use the whole worker without the API ever going quiet.

Concurrency is bounded by lane (app.jobs.kinds.LANES), not by a single number:
"how many jobs at once" is the wrong question when one job is an HTTP wait and
the next is a full-CPU encode.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import time
import uuid
from pathlib import Path

from app.ai import usage as llm_usage
from app.ai.openrouter_client import build_client as build_llm_client
from app.ai.suggest import generate_suggestions
from app.config import get_settings, heavy_threads
from app.db import session_scope
from app.jobs import queue
from app.jobs.kinds import MAX_ATTEMPTS, validate_registry
from app.jobs.queue import JobCancelled, JobHandle
from app.models import Suggestions, Transcript, VideoMeta
from app.store import ProjectNotFound, get_row, load, save
from app.stt.duudlaga_client import build_client as build_stt_client
from app.stt.pipeline import separation_available, transcribe_audio
from app.subtitle.srt import segments_to_srt
from app.utils.logging import get_logger, setup_logging
from app.utils.paths import (
    OutOfSpace,
    clear_all_workdirs,
    clear_job_workdir,
    ensure_free,
    job_workdir,
    purge_trash,
)
from app.video.audio import extract_audio_16k_mono_wav, extract_audio_native_wav
from app.video.capabilities import build_capabilities
from app.video.ffmpeg import discover_ffmpeg
from app.video.probe import generate_thumbnail, probe_video

logger = get_logger(__name__)

POLL_IDLE_SEC = 2.0
REAP_INTERVAL_SEC = 60.0
# A housekeeping step slower than this is worth a line: the calls it makes
# reach a database and a third-party API, and both can stall for minutes.
SLOW_CHORE_SEC = 10.0
HEARTBEAT_SEC = 5.0

# Rough scratch budget per job kind, checked before the job starts rather
# than discovered halfway through as [Errno 28].
DISK_NEED_BYTES = {
    "import_video": 4 * 1024**3,
    "transcribe": 6 * 1024**3,
    "export": 8 * 1024**3,
    "export_all": 12 * 1024**3,
    "suggest": 0,
}

_shutdown = asyncio.Event()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


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
        project = load(db, _project_id(handle))
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


async def handle_transcribe(handle: JobHandle) -> dict:
    binaries = _require_ffmpeg()
    workdir = job_workdir(handle.job_id)

    from app import provider_settings, r2

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

    client = build_stt_client(settings)
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
        project = load(db, _project_id(handle))
        project.transcript = transcript
        save(db, project)

    return {
        "segments": len(transcript.segments),
        "characters": len(transcript.full_text),
        "timings_estimated": transcript.timings_estimated,
        "srt_key": srt_key,
    }


async def handle_suggest(handle: JobHandle) -> dict:
    from app import provider_settings

    with session_scope() as db:
        settings = provider_settings.effective(db)
        project = load(db, _project_id(handle))
    if project.video is None:
        raise RuntimeError("This project has no video")
    if project.transcript is None or not project.transcript.segments:
        raise RuntimeError("Transcribe the video before asking for suggestions")

    await handle.set_progress(0.1, stage="requesting", message="Asking the model for suggestions")
    client = build_llm_client(settings)
    try:
        suggestions: Suggestions = await generate_suggestions(
            client, project.transcript, project.video.duration_sec
        )
    finally:
        await client.aclose()

    handle.raise_if_cancelled()

    with session_scope() as db:
        project = load(db, _project_id(handle))
        project.suggestions = suggestions
        save(db, project)

    return {"shorts": len(suggestions.shorts), "youtube": len(suggestions.youtube)}


async def handle_export_all(handle: JobHandle) -> dict:
    return await _render(handle, all_ideas=True)


async def handle_export(handle: JobHandle) -> dict:
    return await _render(handle, all_ideas=False)


async def _render(handle: JobHandle, *, all_ideas: bool) -> dict:
    from app import brand, r2
    from app.dbmodels import Output
    from app.export.overlay import LogoOverlay
    from app.export.pipeline import render_all_ideas, render_timeline

    binaries = _require_ffmpeg()
    workdir = job_workdir(handle.job_id)
    project_id = _project_id(handle)

    with session_scope() as db:
        project = load(db, project_id)
        project_name = project.name
    if project.video is None:
        raise RuntimeError("This project has no video")

    caps = await build_capabilities(binaries.ffmpeg)

    await handle.set_progress(0.02, stage="download", message="Fetching the video")
    local = workdir / f"source{Path(project.video.source_key).suffix or '.mp4'}"
    await asyncio.to_thread(r2.download_file, project.video.source_key, local)

    out_dir = workdir / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    segments = project.transcript.segments if project.transcript else None

    # The mark is global and the choice is per project, so both have to be
    # true before a byte is fetched. A logo the project turned off must not
    # cost a download, and a project asking for one nobody uploaded renders
    # without it rather than failing (render_timeline says so in the log).
    logo = logo_path = None
    if project.export.logo.enabled:
        with session_scope() as db:
            logo_key = brand.get(db)
        if logo_key:
            logo_path = workdir / f"logo{Path(logo_key).suffix or '.png'}"
            await asyncio.to_thread(r2.download_file, logo_key, logo_path)
            logo = LogoOverlay(
                position=project.export.logo.position,
                width_pct=project.export.logo.width_pct,
                opacity=project.export.logo.opacity,
                margin_pct=project.export.logo.margin_pct,
            )
        else:
            logger.warning("Project asked for the brand logo but none is uploaded")

    if all_ideas:
        if project.suggestions is None or not (
            project.suggestions.shorts or project.suggestions.youtube
        ):
            raise RuntimeError("There are no suggestions to export")
        rendered = await render_all_ideas(
            handle, binaries, str(local), project.suggestions, project.transition,
            crf=project.export.crf, preset=project.export.preset,
            orientation=project.export.orientation, portrait_fill=project.export.portrait_fill,
            supported_xfade=caps.xfade_transitions, container="mp4",
            output_dir=out_dir, job_id=handle.job_id,
            write_srt=project.export.write_srt, burn_subtitles=project.export.burn_subtitles,
            subtitle_style=project.subtitle_style, transcript_segments=segments,
            logo=logo, logo_path=logo_path,
        )
    else:
        if not project.clips:
            raise RuntimeError("The timeline has no clips")
        # The stored clips point at the desktop-era source path; the render
        # must read the copy this worker just downloaded.
        clips = [c.model_copy(update={"source_path": str(local)}) for c in project.clips]
        output_path = out_dir / "export.mp4"
        await render_timeline(
            handle, binaries, clips, project.transition,
            crf=project.export.crf, preset=project.export.preset,
            orientation=project.export.orientation, portrait_fill=project.export.portrait_fill,
            supported_xfade=caps.xfade_transitions, workdir=workdir, output_path=output_path,
            write_srt=project.export.write_srt, burn_subtitles=project.export.burn_subtitles,
            subtitle_style=project.subtitle_style, transcript_segments=segments,
            logo=logo, logo_path=logo_path,
        )
        rendered = [{"kind": "export", "title": project_name, "output_path": str(output_path)}]

    handle.raise_if_cancelled()

    await handle.set_progress(0.9, stage="upload", message="Uploading the results")
    results: list[dict] = []
    counts: dict[str, int] = {}
    for item in rendered:
        path = Path(item["output_path"])
        if not path.is_file():
            continue
        kind = item["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        key = r2.output_key(project_id, kind, counts[kind], "mp4")
        size = await asyncio.to_thread(r2.upload_file, path, key, "video/mp4")

        srt_local = path.with_suffix(".srt")
        srt_key = None
        if srt_local.is_file():
            srt_key = key.rsplit(".", 1)[0] + ".srt"
            await asyncio.to_thread(r2.upload_file, srt_local, srt_key, "text/plain; charset=utf-8")

        with session_scope() as db:
            db.add(
                Output(
                    project_id=project_id,
                    kind=kind,
                    title=item.get("title", ""),
                    r2_key=key,
                    srt_key=srt_key,
                    size_bytes=size,
                )
            )
        results.append({"kind": kind, "title": item.get("title", ""), "key": key, "srt_key": srt_key})

    return {"outputs": results}


HANDLERS = {
    "import_video": handle_import_video,
    "transcribe": handle_transcribe,
    "suggest": handle_suggest,
    "export_all": handle_export_all,
    "export": handle_export,
}


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


def _project_id(handle: JobHandle) -> str:
    if not handle.project_id:
        raise RuntimeError(f"Job {handle.job_id} ({handle.kind}) has no project")
    return handle.project_id


def _require_ffmpeg():
    binaries = discover_ffmpeg()
    if not binaries.available:
        raise RuntimeError("FFmpeg is not installed in this image")
    return binaries


async def _heartbeat_loop(handle: JobHandle) -> None:
    """Keeps the row's liveness moving and notices a cancel request.

    A job that reports no progress for minutes — one long LLM call, say — is
    still alive; without this its row would look stale and be handed to
    another worker while it is still running.
    """
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            await handle.flush()
    except asyncio.CancelledError:
        pass


async def _run_job(job) -> None:
    handle = JobHandle(job.id, job.kind, job.project_id, queue.payload_of(job))
    usage = llm_usage.start()
    beat = asyncio.create_task(_heartbeat_loop(handle))
    started = time.time()
    try:
        need = DISK_NEED_BYTES.get(job.kind, 0)
        if need:
            ensure_free(need)

        result = await HANDLERS[job.kind](handle)

        if handle.cancel_requested:
            await asyncio.to_thread(queue.finish, job.id, state="canceled")
            return

        output = dict(result or {})
        output["elapsed_sec"] = round(time.time() - started, 1)
        # Only present when the job actually spent money — an absent field
        # says "no paid call", which a zero would not.
        if usage.calls:
            output["llm"] = usage.to_dict()
        await asyncio.to_thread(queue.finish, job.id, state="done", output=output)

    except JobCancelled:
        await asyncio.to_thread(queue.finish, job.id, state="canceled")
    except OutOfSpace as e:
        logger.warning("Job %s (%s) deferred: %s", job.id, job.kind, e)
        await asyncio.to_thread(queue.finish, job.id, state="failed", error=str(e))
    except ProjectNotFound:
        await asyncio.to_thread(
            queue.finish, job.id, state="failed", error="The project was deleted"
        )
    except Exception as e:  # noqa: BLE001 - a failing job must be recorded, not crash the loop
        logger.exception("Job %s (%s) failed", job.id, job.kind)
        last_attempt = job.no_retry or job.attempts >= MAX_ATTEMPTS
        await asyncio.to_thread(
            queue.finish,
            job.id,
            state="failed" if last_attempt else "queued",
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        beat.cancel()
        clear_job_workdir(job.id)


async def _housekeeping(settings) -> None:
    """Requeue corpses, drop old rows, abort billed multipart leftovers.

    Never awaited from the loop — see the call site. Each step is timed
    because a stall here used to be invisible: the pass logs nothing unless
    it finds something, so a call blocked for six minutes and a pass with
    nothing to do looked identical from outside.
    """
    try:
        for label, work in (
            ("reaping stale jobs", lambda: queue.reap_stale()),
            ("purging old jobs", lambda: queue.purge_old(settings.job_keep_days)),
            ("deleting retired scratch", purge_trash),
            ("aborting stale uploads", _abort_stale_uploads if settings.r2_enabled else None),
        ):
            if work is None:
                continue
            started = time.time()
            count = await asyncio.to_thread(work)
            elapsed = time.time() - started
            if elapsed > SLOW_CHORE_SEC:
                logger.warning("Housekeeping: %s took %.0fs", label, elapsed)
            if label == "reaping stale jobs" and count:
                logger.warning("Requeued %d job(s) from workers that stopped responding", count)
    except Exception:  # noqa: BLE001 - housekeeping must never kill the loop
        logger.exception("Housekeeping pass failed")


def _abort_stale_uploads() -> int:
    from app import r2

    return r2.abort_stale_uploads()


async def main() -> None:
    setup_logging()
    settings = get_settings()
    validate_registry(HANDLERS)

    threads = heavy_threads()
    # Must be set BEFORE torch is imported anywhere: torch reads these at
    # import time and sizing its pool from the host's core count is what
    # gets the whole container throttled.
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(threads))

    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
    logger.info(
        "Worker %s starting (threads=%d, concurrency=%d, separation=%s)",
        worker_id, threads, settings.worker_concurrency, settings.enable_separation,
    )
    clear_all_workdirs()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown.set)

    running: set[asyncio.Task] = set()
    chores: asyncio.Task | None = None
    last_reap = 0.0

    while not _shutdown.is_set():
        # Housekeeping runs BESIDE the loop, never inside it. Every call it
        # makes can block for minutes — a lock held by a killed worker's
        # connection until TCP gives up, an R2 request that never answers —
        # and inline it sits in front of the first claim, so the queue stops
        # dead with nothing in the log but the startup line. Off to the side,
        # a stuck pass costs housekeeping and nothing else.
        if (chores is None or chores.done()) and time.time() - last_reap > REAP_INTERVAL_SEC:
            last_reap = time.time()
            chores = asyncio.create_task(_housekeeping(settings))

        running = {t for t in running if not t.done()}
        if len(running) >= settings.worker_concurrency:
            await asyncio.sleep(POLL_IDLE_SEC)
            continue

        try:
            job = await asyncio.to_thread(queue.claim, worker_id)
        except Exception:  # noqa: BLE001 - a database blip must not end the worker
            logger.exception("Failed to claim a job")
            await asyncio.sleep(POLL_IDLE_SEC * 2)
            continue

        if job is None:
            await asyncio.sleep(POLL_IDLE_SEC)
            continue

        logger.info("Claimed job %s (%s, lane=%s)", job.id, job.kind, job.lane)
        running.add(asyncio.create_task(_run_job(job)))

    if chores and not chores.done():
        chores.cancel()
    if running:
        logger.info("Shutting down; waiting for %d job(s) to finish", len(running))
        await asyncio.gather(*running, return_exceptions=True)
    logger.info("Worker %s stopped", worker_id)


if __name__ == "__main__":
    asyncio.run(main())
