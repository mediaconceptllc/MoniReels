"""Render pipeline: cut+normalize each clip (already applying final export
encoder settings), then join via concat demuxer (transition duration 0) or an
xfade/acrossfade filtergraph (transition duration > 0).

Hard rules honored here: every ffmpeg run reports progress + is cancelable,
and the final output is only ever produced by renaming a `.part` file after
success — a failed or canceled render never leaves a broken file at the
target path.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.export.normalize import build_audio_filter, build_video_filter, validate_portrait_fill
from app.export.overlay import LogoOverlay, build_burn_args, build_overlay_filter
from app.export.presets import (
    AUDIO_CHANNEL_LAYOUT,
    AUDIO_SAMPLE_RATE,
    build_audio_encoder_args,
    build_video_encoder_args,
    resolve_dimensions,
)
from app.jobs.queue import JobCancelled, JobHandle
from app.models import Segment, SubtitleStyle, Suggestions
from app.subtitle.ass import build_ass_document
from app.subtitle.shift import retime_segments_for_output
from app.subtitle.srt import segments_to_srt
from app.timeline.models import Clip, Transition
from app.transition.filtergraph import TransitionPlan, build_xfade_filtergraph, plan_transition
from app.transition.registry import resolve as resolve_transition
from app.utils.logging import get_logger
from app.utils.paths import ascii_safe_filename, atomic_write_text, job_workdir
from app.video.ffmpeg import FfmpegBinaries, FfmpegCancelled, FfmpegRun
from app.video.probe import probe_video

logger = get_logger(__name__)


def _scaled_progress(
    handle: JobHandle, lo: float, hi: float, stage: str
) -> Callable[[float], Awaitable[None]]:
    async def callback(p: float) -> None:
        await handle.set_progress(lo + p * (hi - lo), stage=stage)

    return callback


async def _cut_and_normalize_clip(
    binaries: FfmpegBinaries,
    handle: JobHandle,
    clip: Clip,
    has_audio: bool,
    width: int,
    height: int,
    fps: float,
    crf: int,
    preset: str,
    orientation: str,
    portrait_fill: str,
    out_path: Path,
    progress_lo: float,
    progress_hi: float,
) -> None:
    vf = build_video_filter(width, height, fps, orientation, portrait_fill)
    duration = clip.end - clip.start

    args = ["-ss", f"{clip.start:.3f}", "-to", f"{clip.end:.3f}", "-i", clip.source_path]
    if has_audio:
        af = build_audio_filter(AUDIO_SAMPLE_RATE, AUDIO_CHANNEL_LAYOUT)
        args += ["-vf", vf, "-af", af]
    else:
        args += [
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout={AUDIO_CHANNEL_LAYOUT}:sample_rate={AUDIO_SAMPLE_RATE}",
            "-vf", vf, "-map", "0:v", "-map", "1:a", "-shortest",
        ]
    args += build_video_encoder_args(crf, preset)
    args += build_audio_encoder_args()
    args += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out_path)]

    run = FfmpegRun()
    handle.set_cancel_hook(run.cancel)
    await run.run(
        binaries.ffmpeg,  # type: ignore[arg-type]
        args,
        expected_duration_sec=duration,
        on_progress=_scaled_progress(handle, progress_lo, progress_hi, "normalize"),
    )


async def _join_concat(
    binaries: FfmpegBinaries,
    handle: JobHandle,
    normalized_paths: list[Path],
    total_duration: float,
    workdir: Path,
    out_path: Path,
) -> None:
    list_path = workdir / "concat_list.txt"
    list_path.write_text("\n".join(f"file '{p.name}'" for p in normalized_paths), encoding="utf-8")

    concat_args = [
        "-f", "concat", "-safe", "0", "-i", "concat_list.txt",
        "-c", "copy", "-movflags", "+faststart", str(out_path),
    ]
    run = FfmpegRun()
    handle.set_cancel_hook(run.cancel)
    await run.run(
        binaries.ffmpeg,  # type: ignore[arg-type]
        concat_args,
        expected_duration_sec=total_duration,
        cwd=workdir,
        on_progress=_scaled_progress(handle, 0.6, 0.9, "join"),
    )


async def _join_xfade(
    binaries: FfmpegBinaries,
    handle: JobHandle,
    normalized_paths: list[Path],
    xfade_name: str,
    plan: TransitionPlan,
    crf: int,
    preset: str,
    out_path: Path,
) -> None:
    filter_complex, v_out, a_out = build_xfade_filtergraph(len(normalized_paths), xfade_name, plan)

    args: list[str] = []
    for p in normalized_paths:
        args += ["-i", str(p)]
    args += ["-filter_complex", filter_complex, "-map", f"[{v_out}]", "-map", f"[{a_out}]"]
    args += build_video_encoder_args(crf, preset)
    args += build_audio_encoder_args()
    args += ["-movflags", "+faststart", str(out_path)]

    run = FfmpegRun()
    handle.set_cancel_hook(run.cancel)
    await run.run(
        binaries.ffmpeg,  # type: ignore[arg-type]
        args,
        expected_duration_sec=plan.final_duration,
        on_progress=_scaled_progress(handle, 0.6, 0.9, "join"),
    )


async def _burn_overlays(
    binaries: FfmpegBinaries,
    handle: JobHandle,
    input_path: Path,
    out_path: Path,
    workdir: Path,
    crf: int,
    preset: str,
    expected_duration: float,
    *,
    subtitles: bool,
    logo: LogoOverlay | None,
    logo_path: Path | None,
    frame_width: int,
    frame_height: int,
) -> None:
    """Draws the brand logo and burns workdir/subs.ass, in ONE pass.

    `subs.ass` is referenced by bare filename with cwd=workdir on purpose —
    passing an absolute Windows path (with a drive letter) into the
    `subtitles=` filter option string breaks on the colon, since ffmpeg's
    filter-option parser also uses ':' as a separator.

    The audio is mapped explicitly because a filter_complex replaces the
    default stream selection: without `-map 0:a?` a logo would silently render
    the short SILENT, and `?` rather than a bare `0:a` because a clip with no
    audio track must not fail the whole export.
    """
    graph = build_overlay_filter(
        logo, subtitles=subtitles, frame_width=frame_width, frame_height=frame_height
    )
    args = build_burn_args(
        str(input_path),
        str(logo_path) if logo_path is not None else None,
        graph,
        encoder_args=build_video_encoder_args(crf, preset),
        audio_args=build_audio_encoder_args(),
        out_path=str(out_path),
    )

    run = FfmpegRun()
    handle.set_cancel_hook(run.cancel)
    await run.run(
        binaries.ffmpeg,  # type: ignore[arg-type]
        args,
        expected_duration_sec=expected_duration,
        cwd=workdir,
        on_progress=_scaled_progress(handle, 0.9, 1.0, "subtitles"),
    )


def _cumulative_starts(durations: list[float]) -> list[float]:
    starts = []
    running = 0.0
    for d in durations:
        starts.append(running)
        running += d
    return starts


def pick_fps_source(clips: list[Clip], transcript_source: str | None) -> Clip:
    """Which clip's frame rate the whole render adopts.

    The CONTENT's, not whatever happens to be first. With a brand intro
    prepended, clips[0] is a title card that may well be 25 or 60fps, and
    every second of the actual video would be resampled to match a few
    seconds of animation nobody is watching for the motion.

    Falls back to the first clip when nothing identifies the source, which is
    what a timeline of nothing but cuts always was.
    """
    if transcript_source is None:
        return clips[0]
    return next((c for c in clips if c.source_path == transcript_source), clips[0])


async def _bracket_with_brand(
    binaries: FfmpegBinaries,
    clips: list[Clip],
    intro_path: Path | None,
    outro_path: Path | None,
) -> list[Clip]:
    """Puts the brand intro in front of the timeline and the outro behind it.

    Here, in render_timeline's own preparation, rather than at either call
    site: a single export and a batch of ideas must bracket identically, and
    two implementations of "and then add the outro" is how one of them ends up
    without it.

    A brand clip is a whole file, so its range is 0..duration. It goes through
    exactly the same normalisation as every other clip — resolution, frame
    rate, pixel format — so a 4K 60fps title card joins a 1080x1920 30fps
    reel without a word from the operator.
    """
    if intro_path is None and outro_path is None:
        return clips

    async def whole_file(path: Path, order: int) -> Clip | None:
        meta = await probe_video(binaries.ffprobe, path)  # type: ignore[arg-type]
        duration = float(meta.get("duration") or 0.0)
        if duration <= 0:
            # A file ffprobe reports as zero-length would join as nothing and
            # can make xfade's offsets negative. Skipping is the safe read.
            logger.warning("Brand clip %s has no duration; leaving it out", path.name)
            return None
        return Clip(
            id=uuid.uuid4().hex, source_path=str(path), start=0.0, end=duration, order=order
        )

    bracketed = list(clips)
    if outro_path is not None:
        outro = await whole_file(outro_path, order=len(clips) + 1)
        if outro is not None:
            bracketed.append(outro)
    if intro_path is not None:
        intro = await whole_file(intro_path, order=-1)
        if intro is not None:
            bracketed.insert(0, intro)
    return bracketed


async def render_timeline(
    handle: JobHandle,
    binaries: FfmpegBinaries,
    clips: list[Clip],
    transition: Transition,
    crf: int,
    preset: str,
    orientation: str,
    portrait_fill: str,
    supported_xfade: list[str],
    workdir: Path,
    output_path: Path,
    write_srt: bool = False,
    burn_subtitles: bool = False,
    subtitle_style: SubtitleStyle | None = None,
    transcript_segments: list[Segment] | None = None,
    logo: LogoOverlay | None = None,
    logo_path: Path | None = None,
    transcript_source: str | None = None,
    intro_path: Path | None = None,
    outro_path: Path | None = None,
) -> Path:
    if not clips:
        raise ValueError("Cannot render an empty clip list")
    # A logo asks for a pass that draws pixels; without the file there is
    # nothing to draw, so the export runs without it rather than failing on
    # an asset the render does not depend on.
    if logo is not None and (logo_path is None or not logo_path.exists()):
        logger.warning("Logo requested but no image is available; rendering without it")
        logo = None

    validate_portrait_fill(portrait_fill)
    width, height = resolve_dimensions(orientation)
    want_subtitles = bool(transcript_segments) and (write_srt or burn_subtitles)

    ordered_clips = sorted(clips, key=lambda c: c.order)
    ordered_clips = await _bracket_with_brand(binaries, ordered_clips, intro_path, outro_path)
    workdir.mkdir(parents=True, exist_ok=True)
    # Keep the extension (output.part.mp4, not output.mp4.part) so ffmpeg's
    # muxer can still infer the output format from the temp filename.
    part_path = output_path.with_name(f"{output_path.stem}.part{output_path.suffix}")
    joined_path = workdir / f"joined{output_path.suffix}"
    normalized_paths: list[Path] = []

    try:
        fps_source = pick_fps_source(ordered_clips, transcript_source)
        first_meta = await probe_video(binaries.ffprobe, Path(fps_source.source_path))  # type: ignore[arg-type]
        target_fps = first_meta["fps"] if first_meta["fps"] > 0 else 30.0

        n = len(ordered_clips)
        clip_durations: list[float] = []
        for i, clip in enumerate(ordered_clips):
            handle.raise_if_cancelled()
            lo, hi = (i / n) * 0.6, ((i + 1) / n) * 0.6
            await handle.set_progress(lo, stage="normalize", message=f"Preparing clip {i + 1}/{n}")

            meta = await probe_video(binaries.ffprobe, Path(clip.source_path))  # type: ignore[arg-type]
            out_path = workdir / f"clip_{i:03d}.mp4"
            await _cut_and_normalize_clip(
                binaries, handle, clip, meta["has_audio"], width, height, target_fps,
                crf, preset, orientation, portrait_fill, out_path, lo, hi,
            )
            normalized_paths.append(out_path)
            clip_durations.append(clip.end - clip.start)

        handle.raise_if_cancelled()
        await handle.set_progress(0.6, stage="join", message="Joining clips")

        # Decided before the join, because it chooses where the join writes.
        # A logo alone is reason enough: it draws on pixels exactly as the
        # subtitles do, and both ride the same single re-encode.
        need_burn_pass = (burn_subtitles and bool(transcript_segments)) or logo is not None
        join_target = joined_path if need_burn_pass else part_path

        if transition.duration <= 0 or n == 1:
            clip_output_starts = _cumulative_starts(clip_durations)
            final_duration = sum(clip_durations)
            await _join_concat(binaries, handle, normalized_paths, final_duration, workdir, join_target)
        else:
            xfade_name = resolve_transition(transition.type, supported_xfade)
            plan = plan_transition(clip_durations, transition.duration)
            clip_output_starts = [0.0, *plan.offsets]
            final_duration = plan.final_duration
            if plan.reduced:
                await handle.set_progress(
                    0.6, stage="join",
                    message=f"Transition duration reduced to {plan.duration:.2f}s to fit the shortest clip",
                )
            await _join_xfade(
                binaries, handle, normalized_paths, xfade_name, plan, crf, preset, join_target
            )

        handle.raise_if_cancelled()

        retimed_segments: list[Segment] = []
        if want_subtitles and transcript_segments:
            retimed_segments = retime_segments_for_output(
                transcript_segments, ordered_clips, clip_output_starts, transcript_source
            )

        if write_srt and retimed_segments:
            atomic_write_text(output_path.with_suffix(".srt"), segments_to_srt(retimed_segments))

        burn_subs = burn_subtitles and bool(retimed_segments)
        if burn_subs or logo is not None:
            if burn_subs:
                ass_content = build_ass_document(retimed_segments, subtitle_style or SubtitleStyle())
                (workdir / "subs.ass").write_text(ass_content, encoding="utf-8")
            await _burn_overlays(
                binaries, handle, joined_path, part_path, workdir, crf, preset, final_duration,
                subtitles=burn_subs, logo=logo, logo_path=logo_path,
                frame_width=width, frame_height=height,
            )
        elif need_burn_pass:
            # Subtitles were requested but there was nothing to burn (e.g. no
            # segment overlapped the kept ranges) — the joined file IS final.
            joined_path.replace(part_path)

        await handle.set_progress(0.95, stage="finalize", message="Finalizing output")
        part_path.replace(output_path)
        await handle.set_progress(1.0, stage="done", message="Render complete")
        return output_path
    except FfmpegCancelled as e:
        raise JobCancelled() from e
    finally:
        if part_path.exists():
            part_path.unlink(missing_ok=True)
        if joined_path.exists():
            joined_path.unlink(missing_ok=True)
        for p in normalized_paths:
            p.unlink(missing_ok=True)
        (workdir / "concat_list.txt").unlink(missing_ok=True)
        (workdir / "subs.ass").unlink(missing_ok=True)


def _idea_clips(video_path: str, suggestions: Suggestions) -> list[tuple[str, str, list[Clip]]]:
    """Every suggested idea as its own (kind, title, clips) render request: a
    reel is its 3-5 non-contiguous cuts stitched together, a youtube idea is
    its keep-ranges stitched together (the render is identical either way -
    render_timeline already handles an N-clip list, falling back to no
    transition needed when N==1).
    """
    ideas: list[tuple[str, str, list[Clip]]] = []
    for short in suggestions.shorts:
        clips = [
            Clip(id=uuid.uuid4().hex, source_path=video_path, start=cut.start, end=cut.end, order=i)
            for i, cut in enumerate(short.cuts)
        ]
        ideas.append(("reel", short.title, clips))
    for plan in suggestions.youtube:
        clips = [
            Clip(id=uuid.uuid4().hex, source_path=video_path, start=r.start, end=r.end, order=i)
            for i, r in enumerate(plan.ranges)
        ]
        ideas.append(("youtube", plan.title, clips))
    return ideas


def pick_ideas(suggestions: Suggestions, payload: dict) -> tuple[Suggestions, list[str]]:
    """Narrow a project's suggestions to the ones a job was queued for.

    An empty or absent selection means everything — which is what the export
    button did before there was any way to choose, so an old queued job and a
    new one behave identically.

    Anything the selection names that is no longer there is SKIPPED and
    returned for the job's warnings, never guessed at. A job can sit in the
    queue while somebody regenerates the suggestions, and rendering whatever
    now occupies index 2 is the one outcome worth ruling out: the producer
    would get a video they never asked for, with no way to tell.
    """
    picked = payload.get("pick") if isinstance(payload, dict) else None
    if not isinstance(picked, dict):
        return suggestions, []

    skipped: list[str] = []
    short_ids = picked.get("shorts")
    if isinstance(short_ids, list):
        by_id = {s.id: s for s in suggestions.shorts}
        shorts = [by_id[i] for i in short_ids if i in by_id]
        skipped += [f"богино видео «{i}» олдсонгүй" for i in short_ids if i not in by_id]
    else:
        shorts = list(suggestions.shorts)

    wanted = picked.get("youtube")
    if isinstance(wanted, list):
        plans = []
        for entry in wanted:
            index = entry.get("i") if isinstance(entry, dict) else None
            title = entry.get("title") if isinstance(entry, dict) else None
            if not isinstance(index, int) or not 0 <= index < len(suggestions.youtube):
                skipped.append(f"YouTube хураангуй №{index} олдсонгүй")
                continue
            plan = suggestions.youtube[index]
            # The position alone is not identity: pin it to the title the
            # selection was made against.
            if title is not None and plan.title != title:
                skipped.append(f"YouTube хураангуй «{title}» өөрчлөгдсөн тул алгаслаа")
                continue
            plans.append(plan)
    else:
        plans = list(suggestions.youtube)

    return Suggestions(shorts=shorts, youtube=plans), skipped


async def render_all_ideas(
    handle: JobHandle,
    binaries: FfmpegBinaries,
    video_path: str,
    suggestions: Suggestions,
    transition: Transition,
    crf: int,
    preset: str,
    orientation: str,
    portrait_fill: str,
    supported_xfade: list[str],
    container: str,
    output_dir: Path,
    job_id: str,
    write_srt: bool = False,
    burn_subtitles: bool = False,
    subtitle_style: SubtitleStyle | None = None,
    transcript_segments: list[Segment] | None = None,
    logo: LogoOverlay | None = None,
    logo_path: Path | None = None,
    transcript_source: str | None = None,
    intro_path: Path | None = None,
    outro_path: Path | None = None,
) -> list[dict]:
    """Renders one standalone output file per suggested idea (up to 3 reels +
    up to 3 youtube compilations), reusing render_timeline unchanged for each.

    Progress is per-idea (0..1 within each render_timeline call, including
    its own final 1.0), not smoothed across the whole batch - it visibly
    resets between ideas rather than climbing smoothly end to end. Simpler
    than threading a progress window through render_timeline's many internal
    set_progress calls, and `stage`/`message` still identify which idea is
    currently rendering.
    """
    ext = "mov" if container == "mov" else "mp4"
    ideas = _idea_clips(video_path, suggestions)
    if not ideas:
        raise ValueError("No suggested ideas to export")

    results: list[dict] = []
    kind_totals = {"reel": len(suggestions.shorts), "youtube": len(suggestions.youtube)}
    kind_counts: dict[str, int] = {}
    n = len(ideas)

    for i, (kind, title, clips) in enumerate(ideas):
        handle.raise_if_cancelled()
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        idx = kind_counts[kind]

        if not clips:
            # A youtube plan can degenerate to zero ranges after clamping
            # (all raw ranges fell outside the video) - skip it rather than
            # let one bad idea fail the whole batch.
            logger.warning("Skipping %s %d/%d (%r): no valid clips", kind, idx, kind_totals[kind], title)
            continue

        logger.info("Exporting idea %d/%d: %s %d/%d (%r)", i + 1, n, kind, idx, kind_totals[kind], title)
        await handle.set_progress(
            i / n, stage="rendering", message=f"Rendering {kind} {idx}/{kind_totals[kind]}"
        )

        filename = f"{kind}_{idx}_{ascii_safe_filename(f'{title}.{ext}')}"
        output_path = output_dir / filename
        idea_workdir = job_workdir(f"{job_id}_{i}")

        await render_timeline(
            handle, binaries, clips, transition,
            crf=crf, preset=preset, orientation=orientation, portrait_fill=portrait_fill,
            supported_xfade=supported_xfade, workdir=idea_workdir, output_path=output_path,
            write_srt=write_srt, burn_subtitles=burn_subtitles, subtitle_style=subtitle_style,
            transcript_segments=transcript_segments, logo=logo, logo_path=logo_path,
            transcript_source=transcript_source, intro_path=intro_path, outro_path=outro_path,
        )
        results.append({"kind": kind, "title": title, "output_path": str(output_path)})

    await handle.set_progress(1.0, stage="done", message="All exports complete")
    return results
