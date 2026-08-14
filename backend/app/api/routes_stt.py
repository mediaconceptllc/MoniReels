"""Transcription: audio extraction + Chimege STT, run as a background job."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_ffmpeg_binaries
from app.config import get_settings
from app.jobs.manager import JobHandle, get_job_manager
from app.store import ProjectNotFound, load_project, save_project
from app.stt.chimege_client import ChimegeClient, ChimegeConfig, ChimegeError
from app.stt.pipeline import ml_deps_available, transcribe_with_voice_separation
from app.subtitle.srt import segments_to_srt
from app.utils.logging import get_logger
from app.utils.paths import atomic_write_text, project_dir
from app.video.audio import extract_audio_16k_mono_wav, extract_audio_native_wav
from app.video.ffmpeg import FfmpegBinaries

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["stt"])


@router.post("/{project_id}/transcribe")
async def transcribe_project(
    project_id: str,
    binaries: FfmpegBinaries = Depends(get_ffmpeg_binaries),  # noqa: B008 - standard FastAPI DI pattern
) -> dict:
    try:
        project = load_project(project_id)
    except ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found") from e
    if project.video is None:
        raise HTTPException(status_code=400, detail="Project has no imported video to transcribe")

    video_path = Path(project.video.path)

    async def worker(handle: JobHandle) -> dict:
        settings = get_settings()
        client = ChimegeClient(
            ChimegeConfig(
                url=settings.chimege_stt_url,
                token=settings.chimege_token,
                max_audio_sec=settings.chimege_max_audio_sec,
            )
        )
        try:
            if ml_deps_available():
                await handle.set_progress(0.05, stage="extract_audio", message="Extracting audio")
                # Kept (not deleted) as project_dir/audio.wav — see routes_project.py's
                # video copy for the same "project directory is self-contained" reasoning.
                audio_path = project_dir(project_id) / "audio.wav"
                await extract_audio_native_wav(binaries.ffmpeg, video_path, audio_path)  # type: ignore[arg-type]

                await handle.set_progress(
                    0.1, stage="separating_vocals", message="Separating voice from music"
                )

                async def on_progress(p: float) -> None:
                    await handle.set_progress(
                        0.1 + p * 0.85, stage="transcribing", message="Transcribing speech"
                    )

                try:
                    # workdir = the project directory itself, not a disposable
                    # temp dir — transcribe_with_voice_separation writes
                    # vocals.wav/music.wav/vad_segments.json/voice_chunks/
                    # straight into it (see app/stt/pipeline.py).
                    transcript = await transcribe_with_voice_separation(
                        client,
                        audio_path,
                        project_dir(project_id),
                        settings,
                        binaries.ffmpeg,  # type: ignore[arg-type]
                        on_progress=on_progress,
                    )
                except ChimegeError as e:
                    raise RuntimeError(f"Transcription failed: {e}") from e
            else:
                await handle.set_progress(0.05, stage="extract_audio", message="Extracting audio")
                audio_path = project_dir(project_id) / "audio.wav"
                await extract_audio_16k_mono_wav(binaries.ffmpeg, video_path, audio_path)  # type: ignore[arg-type]

                await handle.set_progress(0.2, stage="transcribing", message="Sending audio to Chimege")
                try:
                    transcript = await client.transcribe(audio_path)
                except ChimegeError as e:
                    raise RuntimeError(f"Transcription failed: {e}") from e
        finally:
            await client.aclose()

        current = load_project(project_id)
        current.transcript = transcript
        save_project(current)
        atomic_write_text(project_dir(project_id) / "transcript.json", transcript.model_dump_json(indent=2))
        if transcript.segments:
            atomic_write_text(project_dir(project_id) / "subtitles.srt", segments_to_srt(transcript.segments))

        await handle.set_progress(1.0, stage="done", message="Transcription complete")
        return current.model_dump(mode="json")

    job = get_job_manager().start(worker)
    return {"job_id": job.id}
