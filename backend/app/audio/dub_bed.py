"""The scene without its speakers — the bed a Mongolian dub is laid on.

A voice-over keeps the original under the Mongolian at a low level, so the
source language is still heard beneath it. A dub removes it: Demucs splits
each exported range into its sources, the vocal one is dropped, and what is
left — music, effects, the room — is the bed the Mongolian voice is mixed
over. Everything Demucs hears as a voice goes, singing included.

**Only the ranges an export renders are separated, never the whole video.**
Demucs on a CPU takes a sizeable fraction of real time; three one-minute
shorts cut from an hour-long video are three minutes of work, not sixty.

**Each range is separated once** and kept in storage
(`audio/{project}/bed-{hash}.flac`), keyed by the model and the range. A
re-export, a retry after a failed render, another idea over the same cut —
none of them pays the CPU again.

**A second of context either side.** Demucs separates better with the sound
around a cut than with a cut that starts cold, so each range is read padded
and the padding is trimmed off afterwards. The bed is then exactly as long as
the range: sample for sample, on the clip's own timeline (0 = its start).

torch is imported only inside the functions that run the model. The API
image does not have it and must still import this module (`available()` is
what the worker reports about itself), and the worker image built without
INSTALL_DUB=1 must be able to say "not here" rather than crash.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.utils.logging import get_logger

logger = get_logger(__name__)

#: What htdemucs (every Demucs v4 model) is trained on. Checked against the
#: loaded model before anything is separated: a model at another rate would
#: separate the wrong audio without a sound of complaint.
SAMPLE_RATE = 44100
CHANNELS = 2
#: Raw float32, interleaved: the bytes of one frame.
FRAME_BYTES = 4 * CHANNELS
#: Context either side of a range, trimmed off again (see the module note).
PAD_S = 1.0

Separate = Callable[[bytes], bytes]


class BedError(RuntimeError):
    pass


class DubUnavailable(BedError):
    """This worker was built without Demucs. Every attempt would meet the
    same image, so the export ends at the first one rather than retrying."""


def available() -> bool:
    """Whether this process can separate at all. Answered without importing
    torch, which takes seconds and hundreds of megabytes."""
    return all(importlib.util.find_spec(name) is not None for name in ("torch", "demucs"))


def bed_key(project_id: str, model: str, start: float, end: float) -> str:
    """Where the bed for [start, end) of a project's source is kept.

    The model is part of the key: another model is another separation.
    """
    from app import r2

    digest = hashlib.sha256(f"{model}|{start:.3f}|{end:.3f}|{SAMPLE_RATE}".encode()).hexdigest()[:32]
    return r2.audio_key(project_id, f"bed-{digest}.flac")


def range_key(start: float, end: float) -> tuple[float, float]:
    """How a range is looked up: the same cut, however its floats arrived."""
    return (round(start, 3), round(end, 3))


# --------------------------------------------------------------------------
# One range
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    """What is read from the source for one range, and what of it is kept."""

    #: Where reading starts and stops in the source, padding included.
    start: float
    end: float
    #: Frames of padding to drop from the front.
    lead: int
    #: Frames the bed has: exactly the range.
    keep: int


def window(start: float, end: float, pad: float = PAD_S) -> Window:
    begin = max(0.0, start - pad)
    return Window(
        start=begin,
        end=end + pad,
        lead=round((start - begin) * SAMPLE_RATE),
        keep=round((end - start) * SAMPLE_RATE),
    )


def trim(pcm: bytes, w: Window) -> bytes:
    """The range out of its padded read — exactly `w.keep` frames, silence
    where the source ran out before the range did."""
    begin = w.lead * FRAME_BYTES
    size = w.keep * FRAME_BYTES
    body = pcm[begin:begin + size]
    return body + bytes(size - len(body))


async def read_range(ffmpeg: Path, source: Path, w: Window) -> bytes:
    """The window's sound as raw float32 stereo at the model's rate."""
    proc = await asyncio.create_subprocess_exec(
        str(ffmpeg), "-hide_banner", "-loglevel", "error",
        "-ss", f"{w.start:.3f}", "-to", f"{w.end:.3f}", "-i", str(source),
        "-vn", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise BedError(f"Could not read {source.name}: {err.decode(errors='replace')[-300:]}")
    return out[: len(out) - len(out) % FRAME_BYTES]


async def encode_flac(ffmpeg: Path, pcm: bytes, out_path: Path) -> Path:
    """Raw float32 stereo to a 16-bit FLAC — half a WAV in storage, and read
    by the mix directly."""
    proc = await asyncio.create_subprocess_exec(
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-i", "-",
        "-c:a", "flac", "-sample_fmt", "s16", str(out_path),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate(pcm)
    if proc.returncode != 0:
        raise BedError(f"Could not encode {out_path.name}: {err.decode(errors='replace')[-300:]}")
    return out_path


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------

_MODELS: dict[tuple[str, str], Any] = {}


def load_model(name: str, cache_dir: Path) -> Any:
    """The pretrained model, loaded once per process. Its weights come from
    `cache_dir` — baked into the worker image — and are downloaded there
    only when they are missing."""
    key = (name, str(cache_dir))
    if key not in _MODELS:
        import torch
        from demucs.pretrained import get_model

        cache_dir.mkdir(parents=True, exist_ok=True)
        torch.hub.set_dir(str(cache_dir))
        model = get_model(name)
        model.eval()
        _MODELS[key] = model
    return _MODELS[key]


def separate_sync(pcm: bytes, *, model_name: str, cache_dir: Path, threads: int) -> bytes:
    """Raw float32 stereo in, the same length out with every voice removed.

    Normalised the way Demucs' own command line does before separating, and
    restored after: the models were trained on normalised mixes. A silent
    range has nothing to normalise by and nothing to separate — it comes
    back silent without touching the model.
    """
    import torch
    from demucs.apply import apply_model

    frames = len(pcm) // FRAME_BYTES
    if frames == 0:
        return b""
    wav = torch.frombuffer(bytearray(pcm[: frames * FRAME_BYTES]), dtype=torch.float32)
    wav = wav.view(frames, CHANNELS).t()
    ref = wav.mean(0)
    std = float(ref.std()) if frames > 1 else 0.0
    if not math.isfinite(std) or std < 1e-8:
        return bytes(frames * FRAME_BYTES)
    mean = float(ref.mean())

    # The pool is sized from the container's quota, never the host's cores —
    # see app.config.heavy_threads for what happens otherwise.
    torch.set_num_threads(max(1, threads))
    model = load_model(model_name, cache_dir)
    if model.samplerate != SAMPLE_RATE or model.audio_channels != CHANNELS:
        raise BedError(
            f"{model_name} works at {model.samplerate} Hz, {model.audio_channels} channel(s); "
            f"the bed is read at {SAMPLE_RATE} Hz, {CHANNELS}"
        )
    if "vocals" not in model.sources:
        raise BedError(f"{model_name} has no vocal source to remove: {model.sources}")

    with torch.no_grad():
        sources = apply_model(
            model, ((wav - mean) / std)[None], device="cpu", split=True, overlap=0.25,
            progress=False,
        )[0]
    vocals = model.sources.index("vocals")
    rest = [i for i in range(len(model.sources)) if i != vocals]
    bed = sources[rest].sum(dim=0) * std + mean
    return bed.t().contiguous().numpy().astype("<f4").tobytes()


# --------------------------------------------------------------------------
# An export's ranges
# --------------------------------------------------------------------------

@dataclass
class BedReport:
    #: Ranges separated by THIS export — CPU spent — and the seconds of
    #: sound they held.
    separated: int = 0
    seconds: float = 0.0
    #: Ranges already in storage from an earlier export.
    cached: int = 0


async def prepare(
    ffmpeg: Path,
    project_id: str,
    source: Path,
    ranges: list[tuple[float, float]],
    cache_dir: Path,
    *,
    model_name: str,
    separate: Separate,
    check: Callable[[], None] | None = None,
    on_progress: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[dict[tuple[float, float], Path], BedReport]:
    """A bed for every range, as local files: {range_key: flac path}.

    Read from storage when this model has separated this range before;
    separated and stored otherwise — stored the moment it is made, so an
    export that fails on its third range has still kept the first two.
    `check` runs between ranges (a cancel lands there; a range in progress
    cannot be interrupted).
    """
    from app import r2

    wanted: list[tuple[float, float]] = []
    for start, end in ranges:
        key = range_key(start, end)
        if key[1] > key[0] and key not in wanted:
            wanted.append(key)

    cache_dir.mkdir(parents=True, exist_ok=True)
    report = BedReport()
    beds: dict[tuple[float, float], Path] = {}
    for n, (start, end) in enumerate(wanted, 1):
        if check:
            check()
        key = bed_key(project_id, model_name, start, end)
        local = cache_dir / key.rsplit("/", 1)[-1]
        if await asyncio.to_thread(r2.exists, key):
            await asyncio.to_thread(r2.download_file, key, local)
            report.cached += 1
        else:
            w = window(start, end)
            pcm = await read_range(ffmpeg, source, w)
            started = time.monotonic()
            separated = await asyncio.to_thread(separate, pcm)
            logger.info("Separated %.1fs of sound in %.1fs", end - start, time.monotonic() - started)
            await encode_flac(ffmpeg, trim(separated, w), local)
            await asyncio.to_thread(r2.upload_file, local, key, "audio/flac")
            report.separated += 1
            report.seconds += end - start
        beds[(start, end)] = local
        if on_progress:
            await on_progress(n / len(wanted))
    return beds, report


# --------------------------------------------------------------------------
# Build-time and CI checks:  python -m app.audio.dub_bed --fetch | --smoke
# --------------------------------------------------------------------------

def _smoke(model_name: str, cache_dir: Path, threads: int) -> None:
    """Separates six seconds of a synthetic mix with the real model and
    checks what comes back: the one test that proves the image can dub —
    torch, Demucs, the baked weights and this module's own arithmetic
    together."""
    import torch

    seconds = 6
    t = torch.arange(seconds * SAMPLE_RATE, dtype=torch.float32) / SAMPLE_RATE
    tone = 0.3 * torch.sin(2 * math.pi * 110.0 * t)
    noise = 0.05 * torch.randn(len(t), generator=torch.Generator().manual_seed(0))
    mix = torch.stack([tone + noise, tone - noise]).t().contiguous()
    pcm = mix.numpy().astype("<f4").tobytes()

    started = time.monotonic()
    out = separate_sync(pcm, model_name=model_name, cache_dir=cache_dir, threads=threads)
    elapsed = time.monotonic() - started

    if len(out) != len(pcm):
        raise SystemExit(f"bed is {len(out)} bytes for {len(pcm)} in")
    bed = torch.frombuffer(bytearray(out), dtype=torch.float32)
    if not bool(torch.isfinite(bed).all()):
        raise SystemExit("bed has non-finite samples")
    rms = float(bed.pow(2).mean().sqrt())
    if rms <= 1e-4:
        raise SystemExit(f"bed is silent (rms {rms:.6f}); the music was removed with the voice")
    print(f"ok: {seconds}s separated in {elapsed:.1f}s on {threads} thread(s), bed rms {rms:.4f}")


def main(argv: list[str] | None = None) -> None:
    from app.config import get_settings, heavy_threads

    parser = argparse.ArgumentParser(prog="python -m app.audio.dub_bed")
    parser.add_argument("--fetch", action="store_true", help="download the weights into the cache")
    parser.add_argument("--smoke", action="store_true", help="separate a synthetic mix")
    args = parser.parse_args(argv)
    settings = get_settings()
    cache_dir = settings.resolved_model_cache_dir
    if not available():
        raise SystemExit("torch and demucs are not installed (build with INSTALL_DUB=1)")
    if args.fetch:
        load_model(settings.demucs_model, cache_dir)
        print(f"ok: {settings.demucs_model} weights in {cache_dir}")
    if args.smoke:
        _smoke(settings.demucs_model, cache_dir, heavy_threads())


if __name__ == "__main__":
    main()
