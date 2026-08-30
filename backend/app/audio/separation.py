"""Vocal isolation via Demucs (htdemucs) — separates music/background noise
out of extracted audio before VAD and transcription, so neither has to contend with
non-speech content. Both the isolated vocal stem and a combined music/
background stem (drums+bass+other summed) are produced and saved, so the
project directory keeps a record of both halves of the split; the original
audio this runs against is never modified (the caller's file, untouched).

Model weights are fetched by `demucs.pretrained.get_model` via
`torch.hub.load_state_dict_from_url` on first use and cached for every run
after - never bundled with the installer. Redirected to the app's own
model cache dir (see app.config.Settings.resolved_model_cache_dir) via
`torch.hub.set_dir`, instead of torch's default `~/.cache/torch/hub`.

Deliberately avoids `demucs.audio.AudioFile`, which shells out to a
hardcoded `ffmpeg` on PATH - this app discovers its own ffmpeg binary (see
app.video.ffmpeg) and may only have one under backend/bin/, not on PATH.
Runs on pure-torch decode/resample (`torchaudio` + `demucs.audio.
convert_audio`) instead, so no ffmpeg is needed anywhere in this module.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
from pathlib import Path

from app.utils.logging import get_logger

logger = get_logger(__name__)

# The shared input contract of the STT provider and Silero VAD (see app.stt.chunking,
# app.audio.vad) - the vocal stem is downsampled straight to this on the
# way out, so nothing downstream needs its own resample pass.
OUTPUT_SAMPLE_RATE = 16000
OUTPUT_CHANNELS = 1


class SeparationError(Exception):
    pass


def _get_model_silently(model_name: str):
    """This app runs as a headless subprocess spawned by the Flutter
    desktop shell (see frontend/lib/application/backend_launcher.dart)
    with no attached console - stdout/stderr aren't normal writable
    streams there. `demucs.pretrained.get_model` (via `torch.hub.
    load_state_dict_from_url`, used to download htdemucs' weights on first
    use) both prints a "Downloading..." line straight to `sys.stderr` *and*
    drives a tqdm progress bar - either crashes with `OSError: [Errno 22]
    Invalid argument` the instant it writes. Neither is configurable from
    demucs' own call site, so stdout/stderr are redirected to in-memory
    buffers for the duration of the call instead - the only way to
    guarantee nothing anywhere in this call tries to touch the real,
    broken streams.
    """
    from demucs.pretrained import get_model

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return get_model(model_name)


def _separate_vocals_sync(
    audio_path: Path, vocals_out_path: Path, music_out_path: Path, model_cache_dir: Path, model_name: str
) -> None:
    import torch
    import torchaudio
    from demucs.apply import apply_model
    from demucs.audio import convert_audio, save_audio

    model_cache_dir.mkdir(parents=True, exist_ok=True)
    torch.hub.set_dir(str(model_cache_dir))

    model = _get_model_silently(model_name)
    model.eval()

    wav, sr = torchaudio.load(str(audio_path))
    model_input = convert_audio(wav, sr, model.samplerate, model.audio_channels)

    with torch.no_grad():
        estimates = apply_model(model, model_input.unsqueeze(0), device="cpu", progress=False)[0]

    vocals_idx = model.sources.index("vocals")
    vocals = estimates[vocals_idx]
    # "music" here means everything Demucs didn't classify as vocals (drums
    # + bass + other, e.g. htdemucs' 4 sources) summed into one stem, not
    # just the literal "music" source some other Demucs models expose.
    other_indices = [i for i in range(estimates.shape[0]) if i != vocals_idx]
    music = estimates[other_indices].sum(dim=0)

    vocals = convert_audio(vocals, model.samplerate, OUTPUT_SAMPLE_RATE, OUTPUT_CHANNELS)
    music = convert_audio(music, model.samplerate, OUTPUT_SAMPLE_RATE, OUTPUT_CHANNELS)
    save_audio(vocals, str(vocals_out_path), samplerate=OUTPUT_SAMPLE_RATE)
    save_audio(music, str(music_out_path), samplerate=OUTPUT_SAMPLE_RATE)


async def separate_vocals(
    audio_path: Path,
    vocals_out_path: Path,
    music_out_path: Path,
    model_cache_dir: Path,
    model_name: str = "htdemucs",
) -> tuple[Path, Path]:
    """Runs Demucs on `audio_path` (any format torchaudio can load - a plain
    WAV in practice) and writes the isolated, 16kHz-mono vocal stem to
    `vocals_out_path` and the combined non-vocal (music/background) stem to
    `music_out_path`. Blocking model inference is offloaded to a thread so
    the event loop stays free, matching every other long-running step in
    this app's job pipeline.
    """
    logger.info("Separating vocals from %s with %s", audio_path, model_name)
    try:
        await asyncio.to_thread(
            _separate_vocals_sync, audio_path, vocals_out_path, music_out_path, model_cache_dir, model_name
        )
    except Exception as e:  # noqa: BLE001 - surfaced to the caller as a typed error
        raise SeparationError(f"Vocal separation failed for {audio_path}: {e}") from e
    return vocals_out_path, music_out_path


def _resample_to_16k_mono_sync(audio_path: Path, out_path: Path) -> None:
    import torchaudio
    from demucs.audio import convert_audio, save_audio

    wav, sr = torchaudio.load(str(audio_path))
    wav = convert_audio(wav, sr, OUTPUT_SAMPLE_RATE, OUTPUT_CHANNELS)
    save_audio(wav, str(out_path), samplerate=OUTPUT_SAMPLE_RATE)


async def resample_to_16k_mono(audio_path: Path, out_path: Path) -> Path:
    """Downsamples `audio_path` (e.g. the native-quality Demucs input) to
    the 16kHz mono contract shared by VAD and the STT provider, without Demucs' model
    or ffmpeg - used when vocal separation itself couldn't run, so the
    legacy silence-based fallback still gets audio in the format it
    expects instead of the wrong sample rate/channel layout.
    """
    await asyncio.to_thread(_resample_to_16k_mono_sync, audio_path, out_path)
    return out_path
