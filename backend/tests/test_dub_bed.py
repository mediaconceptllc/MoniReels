"""The speech-free bed a clean dub is laid on.

What these hold: a range is read with a second of context either side and
comes back exactly the range, sample for sample, silence where the source
ran out; each range is separated once and kept, and a failure half-way keeps
what it made; a cancel lands between ranges. The separation itself is faked
here — the backend's CI has no torch — and exercised for real, with the
model's own weights, by the worker image's smoke test in CI.
"""
from __future__ import annotations

import asyncio
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from app import r2
from app.audio import dub_bed
from app.audio.dub_bed import (
    CHANNELS,
    FRAME_BYTES,
    PAD_S,
    SAMPLE_RATE,
    Window,
    bed_key,
    prepare,
    range_key,
    trim,
    window,
)

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


# --------------------------------------------------------------------------
# One range
# --------------------------------------------------------------------------

def test_a_range_is_read_with_context_either_side():
    w = window(5.0, 9.0)
    assert (w.start, w.end) == (5.0 - PAD_S, 9.0 + PAD_S)
    assert w.lead == round(PAD_S * SAMPLE_RATE)
    assert w.keep == 4 * SAMPLE_RATE


def test_a_range_at_the_start_takes_what_context_there_is():
    w = window(0.4, 2.0)
    assert w.start == 0.0
    assert w.lead == round(0.4 * SAMPLE_RATE)
    assert w.keep == round(1.6 * SAMPLE_RATE)


def _frames(values: list[float]) -> bytes:
    """One float per frame, the same in both channels."""
    return b"".join(struct.pack("<ff", v, v) for v in values)


def test_the_padding_is_trimmed_off_and_the_range_is_kept_exactly():
    w = Window(start=0.0, end=0.0, lead=2, keep=3)
    kept = trim(_frames([9, 9, 1, 2, 3, 9, 9]), w)
    assert kept == _frames([1, 2, 3])


def test_a_range_the_source_ran_out_in_is_filled_with_silence():
    """The bed is as long as the clip whatever the source had: the mix takes
    its length from it."""
    w = Window(start=0.0, end=0.0, lead=1, keep=4)
    kept = trim(_frames([9, 1, 2]), w)
    assert len(kept) == 4 * FRAME_BYTES
    assert kept == _frames([1, 2, 0, 0])


def test_the_stored_bed_is_named_by_the_model_and_the_range():
    key = bed_key("p1", "htdemucs", 5.0, 9.0)
    assert key.startswith("audio/p1/bed-") and key.endswith(".flac")
    assert key == bed_key("p1", "htdemucs", 5.0, 9.0)
    assert key != bed_key("p1", "htdemucs_ft", 5.0, 9.0)
    assert key != bed_key("p1", "htdemucs", 5.0, 9.5)
    assert key != bed_key("p2", "htdemucs", 5.0, 9.0)


def test_the_same_cut_is_the_same_range_however_its_floats_arrived():
    assert range_key(5.0000001, 8.9999999) == range_key(5.0, 9.0) == (5.0, 9.0)


# --------------------------------------------------------------------------
# An export's ranges
# --------------------------------------------------------------------------

@pytest.fixture
def bucket(monkeypatch):
    objects: dict[str, bytes] = {}
    uploads: list[str] = []

    def exists(key):
        return key in objects

    def download_file(key, local):
        Path(local).write_bytes(objects[key])
        return Path(local)

    def upload_file(local, key, content_type=None):
        objects[key] = Path(local).read_bytes()
        uploads.append(key)
        return len(objects[key])

    monkeypatch.setattr(r2, "exists", exists)
    monkeypatch.setattr(r2, "download_file", download_file)
    monkeypatch.setattr(r2, "upload_file", upload_file)
    return objects, uploads


@pytest.fixture
def plumbing(monkeypatch):
    """No ffmpeg: a read is a second of silence per second asked, and the
    FLAC is the raw bytes it was given."""
    reads: list[Window] = []

    async def read_range(ffmpeg, source, w):
        reads.append(w)
        return bytes(round((w.end - w.start) * SAMPLE_RATE) * FRAME_BYTES)

    async def encode_flac(ffmpeg, pcm, out_path):
        out_path.write_bytes(pcm)
        return out_path

    monkeypatch.setattr(dub_bed, "read_range", read_range)
    monkeypatch.setattr(dub_bed, "encode_flac", encode_flac)
    return reads


class Separator:
    def __init__(self, fail_on: int | None = None) -> None:
        self.calls = 0
        self.fail_on = fail_on

    def __call__(self, pcm: bytes) -> bytes:
        self.calls += 1
        if self.calls == self.fail_on:
            raise dub_bed.BedError("demucs fell over")
        return pcm


def _prepare(tmp_path, ranges, separate, **kw):
    return asyncio.run(prepare(
        Path("ffmpeg"), "p1", Path("src.mp4"), ranges, tmp_path,
        model_name="htdemucs", separate=separate, **kw,
    ))


def test_each_range_is_separated_once(tmp_path, bucket, plumbing):
    objects, uploads = bucket
    separate = Separator()
    beds, report = _prepare(tmp_path, [(5.0, 9.0), (20.0, 26.0), (5.0, 9.0)], separate)

    assert separate.calls == 2
    assert set(beds) == {(5.0, 9.0), (20.0, 26.0)}
    assert (report.separated, report.cached, report.seconds) == (2, 0, 10.0)
    # Stored where the next export looks, each exactly its range long.
    assert uploads == [bed_key("p1", "htdemucs", 5.0, 9.0), bed_key("p1", "htdemucs", 20.0, 26.0)]
    assert len(beds[(5.0, 9.0)].read_bytes()) == 4 * SAMPLE_RATE * FRAME_BYTES


def test_a_range_separated_before_costs_no_cpu(tmp_path, bucket, plumbing):
    """A re-export, another idea over the same cut, a retry after a failed
    render."""
    _prepare(tmp_path / "first", [(5.0, 9.0)], Separator())
    again = Separator()
    beds, report = _prepare(tmp_path / "again", [(5.0, 9.0), (30.0, 31.0)], again)
    assert again.calls == 1
    assert (report.separated, report.cached) == (1, 1)
    assert beds[(5.0, 9.0)].is_file()


def test_a_bed_is_stored_the_moment_it_is_made(tmp_path, bucket, plumbing):
    """So an export that fails on its second range keeps the first."""
    _, uploads = bucket
    with pytest.raises(dub_bed.BedError):
        _prepare(tmp_path, [(5.0, 9.0), (20.0, 26.0)], Separator(fail_on=2))
    assert uploads == [bed_key("p1", "htdemucs", 5.0, 9.0)]


def test_a_cancel_lands_between_ranges(tmp_path, bucket, plumbing):
    checks: list[int] = []

    def check():
        checks.append(1)
        if len(checks) == 2:
            raise RuntimeError("canceled")

    separate = Separator()
    with pytest.raises(RuntimeError, match="canceled"):
        _prepare(tmp_path, [(5.0, 9.0), (20.0, 26.0)], separate, check=check)
    assert separate.calls == 1


def test_an_empty_range_is_not_separated(tmp_path, bucket, plumbing):
    separate = Separator()
    beds, _ = _prepare(tmp_path, [(9.0, 9.0), (9.0, 5.0)], separate)
    assert beds == {} and separate.calls == 0


def test_the_work_is_reported_as_it_goes(tmp_path, bucket, plumbing):
    seen: list[float] = []

    async def on_progress(p):
        seen.append(p)

    _prepare(tmp_path, [(5.0, 9.0), (20.0, 26.0)], Separator(), on_progress=on_progress)
    assert seen == [0.5, 1.0]


# --------------------------------------------------------------------------
# With a real ffmpeg
# --------------------------------------------------------------------------

def _source(path: Path, seconds: float, tone_from: float, tone_to: float) -> Path:
    """A stereo WAV: silence, a 440 Hz tone over [tone_from, tone_to), silence."""
    frames = []
    for i in range(round(seconds * SAMPLE_RATE)):
        t = i / SAMPLE_RATE
        v = int(0.5 * 32767 * math.sin(2 * math.pi * 440 * t)) if tone_from <= t < tone_to else 0
        frames.append(struct.pack("<hh", v, v))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"".join(frames))
    return path


def _decode(path: Path) -> list[int]:
    raw = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        check=True, capture_output=True,
    ).stdout
    return list(struct.unpack(f"<{len(raw) // 2}h", raw))


def _rms(samples: list[int], start: float, end: float) -> float:
    part = samples[round(start * SAMPLE_RATE):round(end * SAMPLE_RATE)]
    return math.sqrt(sum(s * s for s in part) / max(1, len(part))) / 32768


@needs_ffmpeg
def test_the_bed_is_the_range_on_the_clips_own_timeline(tmp_path, bucket):
    """Read through ffmpeg, separated (here: passed through), trimmed and
    stored as FLAC — and it starts where the range does and ends where it
    does. A second of padding left in would delay the whole bed behind the
    picture."""
    src = _source(tmp_path / "src.wav", 12.0, 5.0, 9.0)
    beds, _ = asyncio.run(prepare(
        Path(FFMPEG), "p1", src, [(5.0, 9.0), (10.0, 14.0)], tmp_path / "beds",
        model_name="htdemucs", separate=lambda pcm: pcm,
    ))

    bed = _decode(beds[(5.0, 9.0)])
    assert len(bed) == 4 * SAMPLE_RATE
    # The tone from the first samples to the last: the padding is gone.
    assert _rms(bed, 0.0, 0.05) > 0.3 and _rms(bed, 3.95, 4.0) > 0.3

    # A range running past the end of the source is still its full length.
    tail = _decode(beds[(10.0, 14.0)])
    assert len(tail) == 4 * SAMPLE_RATE and _rms(tail, 0.0, 4.0) < 0.001


@needs_ffmpeg
def test_a_source_without_sound_says_so(tmp_path):
    src = tmp_path / "silent.mp4"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", str(src)],
        check=True,
    )
    with pytest.raises(dub_bed.BedError):
        asyncio.run(dub_bed.read_range(Path(FFMPEG), src, window(0.0, 1.0)))


# --------------------------------------------------------------------------
# The model itself, where it is installed (the worker image)
# --------------------------------------------------------------------------

def test_availability_is_answered_without_importing_torch():
    import sys

    loaded = "torch" in sys.modules
    dub_bed.available()
    assert ("torch" in sys.modules) == loaded


def test_silence_is_separated_without_the_model(monkeypatch):
    """Nothing to normalise by and nothing to remove: the model is never
    loaded for it. Runs where torch is installed."""
    pytest.importorskip("torch")
    monkeypatch.setattr(dub_bed, "load_model", lambda *a: pytest.fail("the model was loaded"))
    silence = bytes(SAMPLE_RATE * FRAME_BYTES)
    assert dub_bed.separate_sync(silence, model_name="htdemucs", cache_dir=Path("."), threads=1) == silence
