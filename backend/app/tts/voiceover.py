"""The Mongolian voice-over: which lines are read, what they sound like, and
where each one goes.

Three questions, answered separately because each fails differently.

**Which lines.** Only the ones an export's clips contain — never the whole
transcript. ElevenLabs bills per character, and a sixty-minute transcript
read aloud to make three one-minute shorts is fifty-seven minutes nobody
hears. A segment belongs to a clip when its middle falls inside it: a cut
lands at a sentence edge, padded a little, so a segment is either wholly in
or a sliver of it is, and the sliver is not what the clip is saying.

**What they sound like.** Each line is synthesised once and kept in storage
(`audio/{project}/voice-{hash}.mp3`), keyed by the voice, the model and the
words. A re-export — another idea, a new logo, a retry after a failed
render — pays for nothing it already has. An edited translation is new
words, so it is a new clip; the old one is left, and goes with the project.

**Where they go.** `place_lines` lays the lines on the clip's own timeline:
each starts where it was said, never over the line before it, played faster
(at most MAX_TEMPO) when it would run into the next one, and cut at the
clip's end. The two languages do not take the same time to say the same
thing, and the translation was fitted to a READING budget, not a speaking
one — so this is where the difference is absorbed, and every line that had
to be hurried or cut is counted in the export's result rather than left for
a viewer to notice. It is arithmetic only, and tested without a sound.
"""
from __future__ import annotations

import asyncio
import hashlib
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.export.presets import AUDIO_SAMPLE_RATE
from app.models import Segment
from app.tts.elevenlabs import ElevenLabsTts
from app.utils.logging import get_logger

logger = get_logger(__name__)

#: Faster than this a voice sounds hurried rather than brisk. A line that
#: still does not fit runs on into the pause after it, or is cut at the end.
MAX_TEMPO = 1.3
#: A breath between two lines the placement had to push together, so they do
#: not read as one sentence.
GAP_S = 0.08
#: A line cut at the clip's end fades out rather than clicks.
FADE_S = 0.12

SAMPLE_RATE = AUDIO_SAMPLE_RATE
BYTES_PER_SECOND = SAMPLE_RATE * 2  # 16-bit mono


class VoiceOverError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Which lines
# --------------------------------------------------------------------------

def lines_in(segments: list[Segment], start: float, end: float) -> list[Segment]:
    """The segments a clip of [start, end) says, in order."""
    return [s for s in segments if start <= (s.start + s.end) / 2 < end]


def clip_key(project_id: str, fingerprint: str, text: str) -> str:
    from app import r2

    digest = hashlib.sha256(f"{fingerprint}\n{text}".encode()).hexdigest()[:32]
    return r2.audio_key(project_id, f"voice-{digest}.mp3")


# --------------------------------------------------------------------------
# Where they go
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Line:
    #: Where it was said, in seconds from the clip's start. Negative for a
    #: segment that began just before the cut.
    at: float
    #: How long the synthesised speech runs at its own pace.
    duration: float


@dataclass(frozen=True)
class Placement:
    start: float
    #: 1.0 at its own pace, up to MAX_TEMPO when it had to be hurried.
    tempo: float
    #: Seconds it occupies on the clip — after the tempo, and after any cut.
    length: float
    #: It ran past the clip's end and was cut there (length 0: not at all).
    cut: bool


def place_lines(lines: list[Line], clip_duration: float) -> list[Placement]:
    """Lays `lines` (in the order they were said) on a clip's timeline.

    A line never starts before it was said, and never over the line before
    it. It is due to be finished when the next line is due to start — or
    when the clip ends — and a line that needs longer is played faster, up
    to MAX_TEMPO. Past that it runs on and pushes the next one later, which
    a pause usually absorbs; what the clip's end does not absorb is cut.
    """
    placements: list[Placement] = []
    # Starts at the clip's own start, so a line said just before the cut
    # begins with the clip rather than before it.
    free_from = 0.0
    for i, line in enumerate(lines):
        start = max(line.at, free_from)
        due = lines[i + 1].at if i + 1 < len(lines) else clip_duration
        room = due - start
        tempo = 1.0
        if line.duration > room:
            tempo = min(MAX_TEMPO, line.duration / room) if room > 0 else MAX_TEMPO
        length = line.duration / tempo
        cut = start + length > clip_duration
        if cut:
            length = max(0.0, clip_duration - start)
        placements.append(Placement(start=start, tempo=tempo, length=length, cut=cut))
        free_from = start + length + GAP_S
    return placements


# --------------------------------------------------------------------------
# What they sound like
# --------------------------------------------------------------------------

@dataclass
class VoiceReport:
    """What the voice-over did, in counts a producer can act on. Goes into
    the export's result as `voice`."""

    #: Segments read aloud across every clip of the export.
    lines: int = 0
    #: Distinct clips paid for by THIS export, and the characters sent for them.
    synthesized: int = 0
    characters: int = 0
    #: Distinct clips that were already in storage — paid for before.
    cached: int = 0
    #: Lines with words and no translation to read: the translation was
    #: cleared after the export was queued. They keep the original sound.
    missing: int = 0
    #: Placements played faster than their own pace to fit, and placements
    #: that reached the end of their clip and were cut there.
    sped_up: int = 0
    cut: int = 0

    def to_dict(self) -> dict:
        return {
            "lines": self.lines,
            "synthesized": self.synthesized,
            "characters": self.characters,
            "cached": self.cached,
            "missing": self.missing,
            "sped_up": self.sped_up,
            "cut": self.cut,
        }


async def prepare(
    client: ElevenLabsTts,
    project_id: str,
    segments: list[Segment],
    ranges: list[tuple[float, float]],
    cache_dir: Path,
    *,
    on_progress: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[dict[str, Path], VoiceReport]:
    """Every line `ranges` contain, as a local clip: {segment id: mp3 path}.

    Read from storage when this voice has said these words before, paid for
    and stored otherwise — stored the moment it arrives, so an export that
    fails half-way has still bought what it bought.
    """
    from app import r2

    report = VoiceReport()
    wanted: dict[str, str] = {}
    missing: set[str] = set()
    for start, end in ranges:
        for seg in lines_in(segments, start, end):
            text = (seg.translation or "").strip()
            if text:
                wanted[seg.id] = text
            elif (seg.text or "").strip():
                missing.add(seg.id)
    report.lines = len(wanted)
    report.missing = len(missing)

    # The same words are one clip, however many lines say them.
    by_text: dict[str, list[str]] = {}
    for seg_id, text in wanted.items():
        by_text.setdefault(text, []).append(seg_id)

    cache_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = client.config.fingerprint()
    paths: dict[str, Path] = {}
    for n, (text, ids) in enumerate(by_text.items(), 1):
        key = clip_key(project_id, fingerprint, text)
        local = cache_dir / key.rsplit("/", 1)[-1]
        if await asyncio.to_thread(r2.exists, key):
            await asyncio.to_thread(r2.download_file, key, local)
            report.cached += 1
        else:
            audio = await client.synthesize(text)
            local.write_bytes(audio)
            await asyncio.to_thread(r2.upload_file, local, key, "audio/mpeg")
            report.synthesized += 1
            report.characters += len(text)
        for seg_id in ids:
            paths[seg_id] = local
        if on_progress:
            await on_progress(n / len(by_text))
    if report.missing:
        logger.warning("%d line(s) have no translation to read; they keep the original sound",
                       report.missing)
    return paths, report


# --------------------------------------------------------------------------
# The track a clip is mixed with
# --------------------------------------------------------------------------

async def decode(
    ffmpeg: Path, source: Path, *, tempo: float = 1.0, length: float | None = None,
    fade: bool = False,
) -> bytes:
    """A clip as 16-bit mono PCM at the render's sample rate — played
    `tempo` times faster, cut to `length` seconds, faded out when cut."""
    filters = []
    if tempo != 1.0:
        filters.append(f"atempo={tempo:.4f}")
    if length is not None:
        filters.append(f"atrim=0:{length:.3f}")
        if fade and length > FADE_S:
            filters.append(f"afade=t=out:st={length - FADE_S:.3f}:d={FADE_S}")
    args = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-af", ",".join(filters) or "anull",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise VoiceOverError(
            f"Could not decode {source.name}: {err.decode(errors='replace')[-300:]}"
        )
    return out[: len(out) - len(out) % 2]


def assemble(pieces: list[tuple[float, bytes]], duration: float) -> bytes:
    """Mono PCM of exactly `duration` seconds, each piece laid at its start.

    Laid, not mixed: `place_lines` never lets two lines overlap, so a piece
    only ever lands on silence. What runs past the end is dropped.
    """
    buf = bytearray(round(duration * SAMPLE_RATE) * 2)
    for start, pcm in pieces:
        at = round(start * SAMPLE_RATE) * 2
        if at >= len(buf):
            continue
        chunk = pcm[: len(buf) - at]
        buf[at: at + len(chunk)] = chunk
    return bytes(buf)


def write_wav(path: Path, pcm: bytes) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return path


@dataclass
class VoiceOver:
    """The prepared voice for one export, and the level the original sound
    keeps under it. `render_timeline` asks it for one track per clip."""

    ffmpeg: Path
    segments: list[Segment]
    audio: dict[str, Path]
    original_volume: float
    report: VoiceReport = field(default_factory=VoiceReport)
    _decoded: dict[Path, bytes] = field(default_factory=dict, repr=False)

    async def _natural(self, path: Path) -> bytes:
        if path not in self._decoded:
            self._decoded[path] = await decode(self.ffmpeg, path)
        return self._decoded[path]

    async def track_for(self, start: float, end: float, out_path: Path) -> Path | None:
        """The voice for the clip [start, end) of the source, as a WAV on the
        clip's own timeline — or None when nothing in it has a voice, so the
        clip renders exactly as it would have without one."""
        said = [s for s in lines_in(self.segments, start, end) if s.id in self.audio]
        if not said:
            return None
        natural = [await self._natural(self.audio[s.id]) for s in said]
        placements = place_lines(
            [Line(at=s.start - start, duration=len(pcm) / BYTES_PER_SECOND)
             for s, pcm in zip(said, natural, strict=True)],
            end - start,
        )
        pieces: list[tuple[float, bytes]] = []
        for seg, pcm, placed in zip(said, natural, placements, strict=True):
            if placed.tempo > 1.0:
                self.report.sped_up += 1
            if placed.cut:
                self.report.cut += 1
            if placed.length <= 0:
                continue
            if placed.tempo > 1.0 or placed.cut:
                pcm = await decode(
                    self.ffmpeg, self.audio[seg.id],
                    tempo=placed.tempo, length=placed.length, fade=placed.cut,
                )
            pieces.append((placed.start, pcm))
        return write_wav(out_path, assemble(pieces, end - start))
