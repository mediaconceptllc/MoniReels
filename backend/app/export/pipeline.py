"""Render pipeline: cut+normalize each clip (already applying final export
encoder settings), then join via concat demuxer (transition duration 0) or an
xfade/acrossfade filtergraph (transition duration > 0) — in batches of
XFADE_BATCH clips for a long timeline (see plan_xfade_batches).

Hard rules honored here: every ffmpeg run reports progress + is cancelable,
and the final output is only ever produced by renaming a `.part` file after
success — a failed or canceled render never leaves a broken file at the
target path.
"""
from __future__ import annotations

import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.export.normalize import (
    build_audio_filter,
    build_video_filter,
    build_voice_mix,
    validate_portrait_fill,
)
from app.export.overlay import LogoOverlay, build_burn_args, build_overlay_filter
from app.export.presets import (
    AUDIO_CHANNEL_LAYOUT,
    AUDIO_SAMPLE_RATE,
    build_audio_encoder_args,
    build_video_encoder_args,
    decoder_thread_args,
    filter_thread_args,
    resolve_dimensions,
)
from app.jobs.queue import JobCancelled, JobHandle
from app.models import Segment, SubtitleStyle, Suggestions
from app.subtitle.ass import build_ass_document
from app.subtitle.shift import retime_segments_for_output
from app.subtitle.srt import segments_to_srt
from app.timeline.models import Clip, Transition
from app.transition.filtergraph import (
    TransitionPlan,
    audio_crossfade_chain,
    build_xfade_filtergraph,
    plan_transition,
    video_xfade_chain,
)
from app.transition.registry import resolve as resolve_transition
from app.utils.logging import get_logger
from app.utils.paths import ascii_safe_filename, atomic_write_text, job_workdir
from app.video.ffmpeg import FfmpegBinaries, FfmpegCancelled, FfmpegRun
from app.video.probe import probe_video

if TYPE_CHECKING:
    from app.tts.voiceover import VoiceOver

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
    voice_path: Path | None = None,
    original_volume: float = 1.0,
    bed_path: Path | None = None,
) -> None:
    vf = build_video_filter(width, height, fps, orientation, portrait_fill)
    duration = clip.end - clip.start

    args = [
        *filter_thread_args(),
        "-ss", f"{clip.start:.3f}", "-to", f"{clip.end:.3f}",
        *decoder_thread_args(), "-i", clip.source_path,
    ]
    if voice_path is not None or bed_path is not None:
        # The voice track and the bed are built on the clip's own timeline
        # (0 = clip.start), so they take no seek of their own. Video keeps its
        # plain -vf; only the sound goes through the graph. The source's own
        # sound is not mapped at all when there is a bed: it still speaks.
        af = build_audio_filter(AUDIO_SAMPLE_RATE, AUDIO_CHANNEL_LAYOUT)
        voice_input = bed_input = None
        if voice_path is not None:
            args += ["-i", str(voice_path)]
            voice_input = 1
        if bed_path is not None:
            args += ["-i", str(bed_path)]
            bed_input = 2 if voice_path is not None else 1
        args += [
            "-map", "0:v", "-vf", vf,
            "-filter_complex", build_voice_mix(
                af, original_volume, has_audio, voice_input=voice_input, bed_input=bed_input,
            ),
            "-map", "[vo_mix]",
        ]
        if not has_audio and bed_path is None:
            args += ["-shortest"]
    elif has_audio:
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


#: Clips one xfade pass may hold. Every input of a filtergraph is decoded at
#: the same time, so one graph over a 28-range YouTube plan ran 28 decoders
#: and every frame in flight between them. A longer timeline is joined in
#: batches of this many and the batches are copied end to end.
XFADE_BATCH = 6
#: A batch boundary cuts THROUGH a clip, in the stretch no transition
#: touches, so the cut is invisible. A clip with less untouched picture than
#: this is never split.
MIN_SPLIT_FRAMES = 4


@dataclass
class XfadeBatch:
    """Clips `first`..`last` (inclusive) of a timeline, joined in one pass.

    A boundary between two batches is a frame inside a clip: the batch before
    shows that clip up to `tail`, the batch after from `head` on. Both are
    the same frame, so nothing is shown twice or lost, and there is no
    transition at the boundary for the copy-join to break.
    """

    first: int
    last: int
    head: int  # first frame of clip `first` this batch shows
    tail: int | None  # frames of clip `last` this batch shows; None = all
    start_frame: int  # where the batch begins in the joined output


def _first_frame(offset: float, fps: float) -> int:
    """The output frame a clip's first frame lands on in a single-pass join.

    xfade starts the transition — and the next clip — at the first frame at
    or after its offset, and the offset it is given is the one written into
    the graph: rounded to the millisecond.
    """
    return math.ceil(round(offset, 3) * fps - 1e-6)


def plan_xfade_batches(
    clip_durations: list[float], plan: TransitionPlan, fps: float, size: int | None = None
) -> list[XfadeBatch]:
    """Splits a joined timeline into passes of at most `size` clips.

    Each boundary falls in the middle of a clip, on a frame, where no
    transition reaches; the batch after it starts on exactly the output frame
    a single pass would show there. A clip too short to split is passed over
    for its neighbour, so a batch can run long rather than cut a transition.
    """
    size = size or XFADE_BATCH
    n = len(clip_durations)
    clear = [(d - 2 * plan.duration) * fps >= MIN_SPLIT_FRAMES for d in clip_durations]
    splits: list[int] = []
    k = 0
    while n - k > size:
        inside = [c for c in range(k + 1, min(k + size, n - 1)) if clear[c]]
        beyond = [c for c in range(k + size, n - 1) if clear[c]]
        if not inside and not beyond:
            break
        k = inside[-1] if inside else beyond[0]
        splits.append(k)

    bounds = [0, *splits, n - 1]
    batches: list[XfadeBatch] = []
    for first, last in zip(bounds, bounds[1:], strict=False):
        head = round(clip_durations[first] / 2 * fps) if batches else 0
        start = (_first_frame(plan.offsets[first - 1], fps) if first else 0) + head
        tail = round(clip_durations[last] / 2 * fps) if last != n - 1 else None
        batches.append(XfadeBatch(first=first, last=last, head=head, tail=tail, start_frame=start))
    return batches


async def _join_xfade(
    binaries: FfmpegBinaries,
    handle: JobHandle,
    normalized_paths: list[Path],
    clip_durations: list[float],
    fps: float,
    xfade_name: str,
    plan: TransitionPlan,
    crf: int,
    preset: str,
    workdir: Path,
    out_path: Path,
) -> None:
    batches = plan_xfade_batches(clip_durations, plan, fps)
    if len(batches) == 1:
        filter_complex, v_out, a_out = build_xfade_filtergraph(len(normalized_paths), xfade_name, plan)
        args = filter_thread_args()
        for p in normalized_paths:
            args += [*decoder_thread_args(len(normalized_paths)), "-i", str(p)]
        args += ["-filter_complex", filter_complex, "-map", f"[{v_out}]", "-map", f"[{a_out}]"]
        args += build_video_encoder_args(crf, preset)
        args += build_audio_encoder_args()
        args += ["-movflags", "+faststart", str(out_path)]
        await _run_join(binaries, handle, args, plan.final_duration, 0.6, 0.9)
        return

    # The sound is ONE pass over every clip: decoding audio holds no frames
    # worth the name, and it is then the very track a single pass would mix —
    # acrossfade has no notion of a batch for its timing to drift across.
    audio_path = workdir / "joined_audio.m4a"
    batch_paths = [workdir / f"batch_{j:03d}.mp4" for j in range(len(batches))]
    try:
        args = []
        for p in normalized_paths:
            # As an input option: the demuxer drops the picture before any
            # decoder sees it.
            args += ["-vn", "-i", str(p)]
        audio = audio_crossfade_chain([f"{i}:a" for i in range(len(normalized_paths))], plan.duration)
        args += ["-filter_complex", ";".join(audio), "-map", "[aout]"]
        args += [*build_audio_encoder_args(), str(audio_path)]
        await _run_join(binaries, handle, args, plan.final_duration, 0.6, 0.62)

        step = 0.26 / len(batches)
        for j, (batch, batch_path) in enumerate(zip(batches, batch_paths, strict=True)):
            handle.raise_if_cancelled()
            args, frames = _batch_args(batch, normalized_paths, plan, fps, xfade_name, crf, preset)
            args.append(str(batch_path))
            await _run_join(
                binaries, handle, args, frames / fps, 0.62 + j * step, 0.62 + (j + 1) * step
            )

        list_path = workdir / "batch_list.txt"
        list_path.write_text("\n".join(f"file '{p.name}'" for p in batch_paths), encoding="utf-8")
        args = [
            "-f", "concat", "-safe", "0", "-i", list_path.name, "-i", str(audio_path),
            "-map", "0:v", "-map", "1:a", "-c", "copy", "-movflags", "+faststart", str(out_path),
        ]
        await _run_join(binaries, handle, args, plan.final_duration, 0.88, 0.9, cwd=workdir)
    finally:
        for p in [audio_path, *batch_paths, workdir / "batch_list.txt"]:
            p.unlink(missing_ok=True)


def _batch_args(
    batch: XfadeBatch,
    normalized_paths: list[Path],
    plan: TransitionPlan,
    fps: float,
    xfade_name: str,
    crf: int,
    preset: str,
) -> tuple[list[str], int]:
    """One batch's video-only pass, and how many frames it ends up holding.

    Its offsets are the single pass's own, less where the batch starts —
    written to the microsecond, so every transition begins on the frame it
    would have begun on in one pass over everything.
    """
    indices = list(range(batch.first, batch.last + 1))
    start = batch.start_frame / fps
    parts: list[str] = []
    labels: list[str] = []
    args = filter_thread_args()
    for i, clip in enumerate(indices):
        args += [*decoder_thread_args(len(indices)), "-i", str(normalized_paths[clip])]
        trims = []
        if clip == batch.first and batch.head:
            trims.append(f"trim=start_frame={batch.head},setpts=PTS-STARTPTS")
        if clip == batch.last and batch.tail is not None:
            trims.append(f"trim=end_frame={batch.tail}")
        if trims:
            parts.append(f"[{i}:v]{','.join(trims)}[p{i}]")
            labels.append(f"p{i}")
        else:
            labels.append(f"{i}:v")
    offsets = [round(plan.offsets[g], 3) - start for g in range(batch.first, batch.last)]
    parts += video_xfade_chain(labels, xfade_name, plan.duration, offsets, digits=6)
    args += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-an"]
    args += build_video_encoder_args(crf, preset)
    args += ["-movflags", "+faststart"]

    if batch.tail is not None:
        frames = _first_frame(plan.offsets[batch.last - 1], fps) + batch.tail - batch.start_frame
    else:
        frames = math.ceil(plan.final_duration * fps) - batch.start_frame
    return args, frames


async def _run_join(
    binaries: FfmpegBinaries,
    handle: JobHandle,
    args: list[str],
    duration: float,
    lo: float,
    hi: float,
    cwd: Path | None = None,
) -> None:
    run = FfmpegRun()
    handle.set_cancel_hook(run.cancel)
    await run.run(
        binaries.ffmpeg,  # type: ignore[arg-type]
        args,
        expected_duration_sec=duration,
        cwd=cwd,
        on_progress=_scaled_progress(handle, lo, hi, "join"),
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
    args = [*filter_thread_args(), *decoder_thread_args()] + build_burn_args(
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
    voice: VoiceOver | None = None,
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
    voice_paths: list[Path] = []

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
            # Only the content speaks Mongolian: a brand intro or outro is
            # not a range of the source and has no lines in it — nor any
            # speech of the source's to remove.
            voice_path = bed_path = None
            if voice is not None and clip.source_path == transcript_source:
                voice_path = await voice.track_for(
                    clip.start, clip.end, workdir / f"voice_{i:03d}.wav"
                )
                if voice_path is not None:
                    voice_paths.append(voice_path)
                bed_path = voice.bed_for(clip.start, clip.end)
            await _cut_and_normalize_clip(
                binaries, handle, clip, meta["has_audio"], width, height, target_fps,
                crf, preset, orientation, portrait_fill, out_path, lo, hi,
                voice_path=voice_path,
                original_volume=voice.original_volume if voice is not None else 1.0,
                bed_path=bed_path,
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
                binaries, handle, normalized_paths, clip_durations, target_fps, xfade_name, plan,
                crf, preset, workdir, join_target,
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
        for p in normalized_paths + voice_paths:
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
    voice: VoiceOver | None = None,
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
            voice=voice,
        )
        results.append({"kind": kind, "title": title, "output_path": str(output_path)})

    await handle.set_progress(1.0, stage="done", message="All exports complete")
    return results
