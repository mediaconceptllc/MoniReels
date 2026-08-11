"""Render pipeline: cut+normalize each clip (already applying final export
encoder settings), then join via concat demuxer (transition duration 0) or an
xfade/acrossfade filtergraph (transition duration > 0).

Hard rules honored here: every ffmpeg run reports progress + is cancelable,
and the final output is only ever produced by renaming a `.part` file after
success — a failed or canceled render never leaves a broken file at the
target path.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from app.export.normalize import build_audio_filter, build_video_filter, validate_portrait_fill
from app.export.presets import (
    AUDIO_CHANNEL_LAYOUT,
    AUDIO_SAMPLE_RATE,
    build_audio_encoder_args,
    build_video_encoder_args,
    choose_encoder,
    resolve_dimensions,
)
from app.jobs.manager import JobCancelled, JobHandle
from app.timeline.models import Clip, Transition
from app.transition.filtergraph import TransitionPlan, build_xfade_filtergraph, plan_transition
from app.transition.registry import resolve as resolve_transition
from app.utils.logging import get_logger
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
    encoder: str,
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
    args += build_video_encoder_args(encoder, crf, preset)
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
    part_path: Path,
) -> None:
    list_path = workdir / "concat_list.txt"
    list_path.write_text("\n".join(f"file '{p.name}'" for p in normalized_paths), encoding="utf-8")

    concat_args = [
        "-f", "concat", "-safe", "0", "-i", "concat_list.txt",
        "-c", "copy", "-movflags", "+faststart", str(part_path),
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
    encoder: str,
    part_path: Path,
) -> None:
    filter_complex, v_out, a_out = build_xfade_filtergraph(len(normalized_paths), xfade_name, plan)

    args: list[str] = []
    for p in normalized_paths:
        args += ["-i", str(p)]
    args += ["-filter_complex", filter_complex, "-map", f"[{v_out}]", "-map", f"[{a_out}]"]
    args += build_video_encoder_args(encoder, crf, preset)
    args += build_audio_encoder_args()
    args += ["-movflags", "+faststart", str(part_path)]

    run = FfmpegRun()
    handle.set_cancel_hook(run.cancel)
    await run.run(
        binaries.ffmpeg,  # type: ignore[arg-type]
        args,
        expected_duration_sec=plan.final_duration,
        on_progress=_scaled_progress(handle, 0.6, 0.9, "join"),
    )


async def render_timeline(
    handle: JobHandle,
    binaries: FfmpegBinaries,
    clips: list[Clip],
    transition: Transition,
    crf: int,
    preset: str,
    orientation: str,
    portrait_fill: str,
    use_hwaccel: bool,
    working_hwaccel_encoder: str | None,
    supported_xfade: list[str],
    workdir: Path,
    output_path: Path,
) -> Path:
    if not clips:
        raise ValueError("Cannot render an empty clip list")

    validate_portrait_fill(portrait_fill)
    width, height = resolve_dimensions(orientation)
    encoder = choose_encoder(use_hwaccel, working_hwaccel_encoder)

    ordered_clips = sorted(clips, key=lambda c: c.order)
    workdir.mkdir(parents=True, exist_ok=True)
    # Keep the extension (output.part.mp4, not output.mp4.part) so ffmpeg's
    # muxer can still infer the output format from the temp filename.
    part_path = output_path.with_name(f"{output_path.stem}.part{output_path.suffix}")
    normalized_paths: list[Path] = []

    try:
        first_meta = await probe_video(binaries.ffprobe, Path(ordered_clips[0].source_path))  # type: ignore[arg-type]
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
                crf, preset, orientation, portrait_fill, encoder, out_path, lo, hi,
            )
            normalized_paths.append(out_path)
            clip_durations.append(clip.end - clip.start)

        handle.raise_if_cancelled()
        await handle.set_progress(0.6, stage="join", message="Joining clips")

        if transition.duration <= 0 or n == 1:
            await _join_concat(binaries, handle, normalized_paths, sum(clip_durations), workdir, part_path)
        else:
            xfade_name = resolve_transition(transition.type, supported_xfade)
            plan = plan_transition(clip_durations, transition.duration)
            if plan.reduced:
                await handle.set_progress(
                    0.6, stage="join",
                    message=f"Transition duration reduced to {plan.duration:.2f}s to fit the shortest clip",
                )
            await _join_xfade(
                binaries, handle, normalized_paths, xfade_name, plan, crf, preset, encoder, part_path
            )

        handle.raise_if_cancelled()
        await handle.set_progress(0.95, stage="finalize", message="Finalizing output")
        part_path.replace(output_path)
        await handle.set_progress(1.0, stage="done", message="Render complete")
        return output_path
    except FfmpegCancelled as e:
        raise JobCancelled() from e
    finally:
        if part_path.exists():
            part_path.unlink(missing_ok=True)
        for p in normalized_paths:
            p.unlink(missing_ok=True)
        concat_list = workdir / "concat_list.txt"
        concat_list.unlink(missing_ok=True)
