"""`export` and `export_all`: the timeline, or the suggested ideas, rendered
and uploaded — with the Mongolian voice and the speech-free beds under it
made first, when the project asks for them."""
from __future__ import annotations

import asyncio
import functools
from pathlib import Path

from app import provider_settings
from app.audio import dub_bed
from app.config import get_settings, heavy_threads
from app.db import session_scope
from app.handlers.common import _project_id, _require_ffmpeg
from app.jobs.queue import JobHandle
from app.languages import removes_source_speech, subtitle_segments, voice_over_on
from app.store import load
from app.tts import voiceover
from app.tts.elevenlabs import build_client as build_tts_client
from app.utils.logging import get_logger
from app.utils.paths import job_workdir
from app.video.capabilities import build_capabilities
from app.video.probe import ProbeError, probe_video

logger = get_logger(__name__)


async def handle_export_all(handle: JobHandle) -> dict:
    return await _render(handle, all_ideas=True)


async def handle_export(handle: JobHandle) -> dict:
    return await _render(handle, all_ideas=False)


async def _render(handle: JobHandle, *, all_ideas: bool) -> dict:
    from sqlalchemy import select

    from app import brand, r2
    from app.dbmodels import Output
    from app.export.ideas import idea_ranges, pick_ideas
    from app.export.overlay import LogoOverlay
    from app.export.pipeline import RenderSpec, render_all_ideas, render_timeline

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
    segments, subtitle_fallback = subtitle_segments(project)

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

    async def _brand_clip(asset: str) -> Path | None:
        """Fetches an intro/outro if the project wants one and it exists.

        A missing asset is a warning, never a failure: an export must not die
        over a title card, and the alternative is a producer discovering at
        render time that the outro nobody uploaded has cost them the render.
        """
        with session_scope() as db:
            key = brand.get(db, asset)
        if not key:
            logger.warning("Project asked for the brand %s but none is uploaded", asset)
            return None
        path = workdir / f"{asset}{Path(key).suffix or '.mp4'}"
        await asyncio.to_thread(r2.download_file, key, path)
        return path

    intro_path = await _brand_clip("intro") if project.export.use_intro else None
    outro_path = await _brand_clip("outro") if project.export.use_outro else None

    skipped: list[str] = []
    if all_ideas:
        if project.suggestions is None or not (
            project.suggestions.shorts or project.suggestions.youtube
        ):
            raise RuntimeError("There are no suggestions to export")
        # A job queued for SOME of the ideas carries which ones. An empty
        # selection is every one of them, so a job queued before choosing
        # existed renders exactly what it always did.
        wanted, skipped = pick_ideas(project.suggestions, handle.payload)
        if not (wanted.shorts or wanted.youtube):
            raise RuntimeError(
                "None of the selected ideas are still in this project: "
                + "; ".join(skipped)
            )
        ranges = idea_ranges(wanted)
    else:
        if not project.clips:
            raise RuntimeError("The timeline has no clips")
        # The stored clips point at the desktop-era source path; the render
        # must read the copy this worker just downloaded.
        clips = [c.model_copy(update={"source_path": str(local)}) for c in project.clips]
        ranges = [(c.start, c.end) for c in clips]

    # Only the lines these ranges say, and before the render: a voice that
    # cannot be made fails the export while it has cost a download, not a
    # render. Each clip bought is stored the moment it arrives, so a retry
    # pays only for what the failed attempt did not get.
    voice = (
        await _prepare_voice(handle, project, ranges, workdir, binaries, local)
        if voice_over_on(project) and project.transcript is not None
        else None
    )

    spec = RenderSpec(
        transition=project.transition,
        crf=project.export.crf, preset=project.export.preset,
        orientation=project.export.orientation, portrait_fill=project.export.portrait_fill,
        supported_xfade=caps.xfade_transitions,
        write_srt=project.export.write_srt, burn_subtitles=project.export.burn_subtitles,
        subtitle_style=project.subtitle_style, transcript_segments=segments,
        logo=logo, logo_path=logo_path, transcript_source=str(local),
        intro_path=intro_path, outro_path=outro_path, voice=voice,
    )
    if all_ideas:
        rendered = await render_all_ideas(
            handle, binaries, str(local), wanted, spec,
            container="mp4", output_dir=out_dir, job_id=handle.job_id,
        )
    else:
        output_path = out_dir / "export.mp4"
        await render_timeline(handle, binaries, clips, spec, workdir=workdir, output_path=output_path)
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
        key = r2.output_key(project_id, handle.job_id, kind, counts[kind], "mp4")
        duration = await _rendered_duration(binaries, path)
        size = await asyncio.to_thread(r2.upload_file, path, key, "video/mp4")

        srt_local = path.with_suffix(".srt")
        srt_key = None
        if srt_local.is_file():
            srt_key = key.rsplit(".", 1)[0] + ".srt"
            await asyncio.to_thread(r2.upload_file, srt_local, srt_key, "text/plain; charset=utf-8")

        title = output_title(item.get("title", ""))
        with session_scope() as db:
            # A retry of this job uploads to the same keys, and its earlier
            # attempt may have recorded some of them before it failed: update
            # that row rather than listing the same video twice.
            row = db.scalar(
                select(Output).where(Output.project_id == project_id, Output.r2_key == key)
            )
            if row is None:
                db.add(
                    Output(
                        project_id=project_id,
                        kind=kind,
                        title=title,
                        r2_key=key,
                        srt_key=srt_key,
                        size_bytes=size,
                        duration_sec=duration,
                    )
                )
            else:
                row.title = title
                row.srt_key = srt_key
                row.size_bytes = size
                row.duration_sec = duration
        results.append({"kind": kind, "title": item.get("title", ""), "key": key, "srt_key": srt_key})

    out: dict = {"outputs": results}
    # An idea that could not be found is NAMED, never dropped quietly: the
    # producer picked three and has to be able to see they got two.
    if skipped:
        out["skipped"] = skipped
    # Same rule for a subtitle line that went out in the source language
    # because its translation was cleared after the export was queued.
    if subtitle_fallback:
        out["subtitle_fallback_lines"] = subtitle_fallback
    # What the voice cost in characters, what it already had, and every line
    # it had to hurry or cut — the parts of a voice-over a producer should
    # listen to before publishing.
    if voice is not None:
        out["voice"] = voice.report.to_dict()
    return out


def output_title(text: str) -> str:
    """A title the outputs row can hold.

    The model's title has no length limit (audit REL-9): one past the
    column failed the job at its very end, after every file had been
    rendered and uploaded. Shortened at a word where one is near, with an
    ellipsis so it reads as shortened; the idea keeps its whole title.
    """
    from sqlalchemy import String

    from app.dbmodels import Output

    # The column's own width, so the two cannot drift apart.
    column = Output.__table__.c.title.type
    if not isinstance(column, String) or column.length is None:
        raise TypeError("outputs.title must be a String with a length")
    limit = column.length
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 3 // 4:
        cut = cut[:space]
    return cut.rstrip() + "…"


async def _rendered_duration(binaries, path: Path) -> float:
    """How long the file that was rendered actually plays.

    Probed, not added up from the plan: a crossfade overlaps its two clips, an
    intro and outro add their own length, and the encoder rounds to frames —
    a total of the cut ranges is a different number from the video's.

    0.0 — the column's own "not measured", as on every output recorded before
    this was written — when ffprobe cannot read it. The file was rendered and
    is about to be uploaded; refusing it over a missing number would throw
    the render away.
    """
    try:
        return round((await probe_video(binaries.ffprobe, path))["duration_sec"], 3)
    except (ProbeError, OSError) as e:
        logger.warning("Could not measure %s: %s", path.name, e)
        return 0.0


async def _prepare_voice(
    handle: JobHandle,
    project,
    ranges: list[tuple[float, float]],
    workdir: Path,
    binaries,
    source: Path,
) -> voiceover.VoiceOver:
    remove_speech = removes_source_speech(project)
    # Before a character is paid for: the voice would be bought for an export
    # that cannot be made the way it was asked for. The page is told this
    # before it queues (app.worker_report); an export queued before this
    # worker was deployed without Demucs is the case that reaches here.
    if remove_speech and not dub_bed.available():
        raise dub_bed.DubUnavailable(
            "Эх яриаг арилгах Demucs энэ worker-т суугаагүй байна — worker сервисийг "
            "INSTALL_DUB=1-ээр дахин build хийнэ."
        )

    with session_scope() as db:
        settings = provider_settings.effective(db)

    async def on_progress(p: float) -> None:
        await handle.set_progress(0.03 + p * 0.07, stage="voicing",
                                  message="Synthesising the Mongolian voice")

    await handle.set_progress(0.03, stage="voicing", message="Synthesising the Mongolian voice")
    client = build_tts_client(settings)
    try:
        audio, report = await voiceover.prepare(
            client, _project_id(handle), project.transcript.segments, ranges,
            workdir / "voice", voices=project.export.speaker_voices, on_progress=on_progress,
        )
    finally:
        await client.aclose()
    voice = voiceover.VoiceOver(
        ffmpeg=binaries.ffmpeg,
        segments=project.transcript.segments,
        audio=audio,
        original_volume=project.export.original_volume,
        report=report,
        remove_speech=remove_speech,
    )
    if remove_speech:
        voice.beds, beds = await _prepare_beds(handle, ranges, workdir, binaries, source)
        report.beds_separated = beds.separated
        report.bed_seconds = beds.seconds
        report.beds_cached = beds.cached
    return voice


async def _prepare_beds(
    handle: JobHandle, ranges: list[tuple[float, float]], workdir: Path, binaries, source: Path
) -> tuple[dict[tuple[float, float], Path], dub_bed.BedReport]:
    """The rendered ranges without their speech, separated before the render
    for the reason the voice is: a failure costs a download, not a render."""
    settings = get_settings()

    async def on_progress(p: float) -> None:
        await handle.set_progress(0.10 + p * 0.10, stage="separating",
                                  message="Removing the source speech")

    await handle.set_progress(0.10, stage="separating", message="Removing the source speech")
    separate = functools.partial(
        dub_bed.separate_sync,
        model_name=settings.demucs_model,
        cache_dir=settings.resolved_model_cache_dir,
        threads=heavy_threads(),
    )
    return await dub_bed.prepare(
        binaries.ffmpeg, _project_id(handle), source, ranges, workdir / "beds",
        model_name=settings.demucs_model, separate=separate,
        check=handle.raise_if_cancelled, on_progress=on_progress,
    )
