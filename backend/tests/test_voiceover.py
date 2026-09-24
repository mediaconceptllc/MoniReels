"""The Mongolian voice-over.

What these hold: only the lines an export's clips contain are paid for, and
each is paid for once; a line is laid where it was said, never over the line
before it, hurried only so far and cut only at the clip's end; and — with a
real ffmpeg, where the machine has one — the voice is actually heard over the
original, and the original is actually kept at the level asked for.
"""
from __future__ import annotations

import asyncio
import math
import shutil
import subprocess
import wave
from array import array
from pathlib import Path

import pytest

from app import r2
from app.models import Segment
from app.tts import voiceover
from app.tts.elevenlabs import TtsError, VoiceConfig
from app.tts.voiceover import (
    GAP_S,
    MAX_TEMPO,
    SAMPLE_CHARS,
    SAMPLE_RATE,
    Line,
    VoiceOver,
    assemble,
    clip_key,
    lines_in,
    place_lines,
    prepare,
    speakers,
    write_wav,
)

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


def _seg(
    i: int, start: float, end: float, translation: str | None = None, text: str = "Hello.",
    speaker: str | None = None,
) -> Segment:
    return Segment(id=f"s{i}", start=start, end=end, text=text, translation=translation,
                   speaker=speaker)


# --------------------------------------------------------------------------
# Where the lines go
# --------------------------------------------------------------------------

def test_a_line_that_fits_starts_where_it_was_said_at_its_own_pace():
    (placed,) = place_lines([Line(at=1.0, duration=2.0)], clip_duration=10.0)
    assert (placed.start, placed.tempo, placed.length, placed.cut) == (1.0, 1.0, 2.0, False)


def test_a_line_too_long_for_its_slot_is_hurried_to_fit():
    first, _ = place_lines([Line(at=0.0, duration=2.4), Line(at=2.0, duration=1.0)], 10.0)
    assert first.tempo == pytest.approx(1.2)
    assert first.length == pytest.approx(2.0)


def test_a_line_is_hurried_no_further_than_the_ceiling():
    first, second = place_lines([Line(at=0.0, duration=4.0), Line(at=1.0, duration=1.0)], 10.0)
    assert first.tempo == MAX_TEMPO
    # ...and the next line waits for it rather than talking over it.
    assert second.start == pytest.approx(first.length + GAP_S)


def test_lines_never_overlap_however_they_arrive():
    lines = [Line(at=a, duration=d) for a, d in
             [(0.0, 3.0), (0.5, 2.0), (0.6, 0.4), (4.0, 5.0), (4.2, 0.3), (9.0, 2.0)]]
    placed = place_lines(lines, clip_duration=12.0)
    for before, after in zip(placed, placed[1:], strict=False):
        assert after.start >= before.start + before.length
    assert all(p.tempo <= MAX_TEMPO for p in placed)


def test_a_line_never_starts_before_it_was_said():
    _, second = place_lines([Line(at=0.0, duration=0.5), Line(at=3.0, duration=0.5)], 10.0)
    assert second.start == 3.0


def test_a_line_that_began_before_the_cut_starts_at_the_clip():
    (placed,) = place_lines([Line(at=-0.3, duration=1.0)], 5.0)
    assert placed.start == 0.0


def test_the_last_line_is_hurried_to_the_clip_end_then_cut_there():
    (hurried,) = place_lines([Line(at=8.0, duration=2.4)], clip_duration=10.0)
    assert hurried.tempo == pytest.approx(1.2) and not hurried.cut

    (cut,) = place_lines([Line(at=8.0, duration=4.0)], clip_duration=10.0)
    assert cut.tempo == MAX_TEMPO and cut.cut
    assert cut.length == pytest.approx(2.0)


def test_a_line_pushed_past_the_end_is_not_placed_and_is_counted():
    _, second = place_lines([Line(at=0.0, duration=13.0), Line(at=5.0, duration=1.0)], 10.0)
    assert second.cut and second.length == 0.0


def test_a_segment_belongs_to_the_clip_its_middle_falls_in():
    segments = [_seg(0, 0.0, 2.0), _seg(1, 4.5, 6.5), _seg(2, 9.5, 12.0)]
    # Starts before the cut but is mostly inside: in. Mostly after the end: out.
    assert [s.id for s in lines_in(segments, 5.0, 10.0)] == ["s1"]
    assert [s.id for s in lines_in(segments, 5.6, 11.0)] == ["s2"]


def test_the_stored_clip_is_named_by_the_voice_and_the_words():
    a = VoiceConfig(api_key="k", voice_id="V1").fingerprint()
    b = VoiceConfig(api_key="k", voice_id="V2").fingerprint()
    key = clip_key("p1", a, "Сайн уу")
    assert key.startswith("audio/p1/voice-") and key.endswith(".mp3")
    assert key == clip_key("p1", a, "Сайн уу")
    assert key != clip_key("p1", b, "Сайн уу")
    assert key != clip_key("p1", a, "Баяртай")
    assert key != clip_key("p2", a, "Сайн уу")


def test_pieces_are_laid_at_their_start_and_the_track_is_exactly_the_clip():
    one = b"\x01\x00" * SAMPLE_RATE  # a second of samples
    track = assemble([(0.5, one), (3.9, one)], duration=4.0)
    assert len(track) == 4 * SAMPLE_RATE * 2
    samples = array("h", track)
    assert samples[int(0.49 * SAMPLE_RATE)] == 0
    assert samples[int(0.51 * SAMPLE_RATE)] == 1
    assert samples[int(1.51 * SAMPLE_RATE)] == 0
    # The second piece runs past the end: kept up to it, the rest dropped.
    assert samples[-1] == 1


# --------------------------------------------------------------------------
# What is paid for
# --------------------------------------------------------------------------

class FakeTts:
    def __init__(self, fail_on: str | None = None) -> None:
        self.config = VoiceConfig(api_key="k", voice_id="V1")
        self.said: list[str] = []
        #: (voice id, words), for every clip bought.
        self.calls: list[tuple[str | None, str]] = []
        self.fail_on = fail_on

    async def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        if text == self.fail_on:
            raise TtsError("quota", status=401, code="quota_exceeded")
        self.said.append(text)
        self.calls.append((voice_id, text))
        return f"mp3:{text}".encode()


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


TRANSCRIPT = [
    _seg(0, 0.0, 2.0, "Нэг."),
    _seg(1, 2.0, 4.0, "Хоёр."),
    _seg(2, 10.0, 12.0, "Гурав."),
    _seg(3, 20.0, 22.0, "Дөрөв."),
]


def test_only_the_lines_the_clips_say_are_paid_for(tmp_path, bucket):
    """ElevenLabs bills per character. The transcript read aloud to make one
    short is everything nobody hears."""
    client = FakeTts()
    paths, report = asyncio.run(
        prepare(client, "p1", TRANSCRIPT, [(0.0, 4.0), (10.0, 12.0)], tmp_path)
    )
    assert client.said == ["Нэг.", "Хоёр.", "Гурав."]
    assert set(paths) == {"s0", "s1", "s2"}
    assert (report.lines, report.synthesized, report.characters) == (3, 3, len("Нэг.Хоёр.Гурав."))


def test_the_same_words_are_one_clip(tmp_path, bucket):
    segments = [_seg(0, 0.0, 1.0, "Тийм."), _seg(1, 1.0, 2.0, "Тийм.")]
    client = FakeTts()
    paths, report = asyncio.run(prepare(client, "p1", segments, [(0.0, 2.0)], tmp_path))
    assert client.said == ["Тийм."]
    assert paths["s0"] == paths["s1"] and report.lines == 2 and report.synthesized == 1


def test_a_line_this_voice_already_said_is_not_paid_for_again(tmp_path, bucket):
    """A re-export, another idea, a retry after a failed render."""
    objects, _ = bucket
    client = FakeTts()
    asyncio.run(prepare(client, "p1", TRANSCRIPT, [(0.0, 4.0)], tmp_path / "first"))
    again = FakeTts()
    paths, report = asyncio.run(prepare(again, "p1", TRANSCRIPT, [(0.0, 4.0)], tmp_path / "again"))
    assert again.said == []
    assert (report.cached, report.synthesized, report.characters) == (2, 0, 0)
    assert paths["s0"].read_bytes() == "mp3:Нэг.".encode()


def test_a_clip_is_stored_the_moment_it_arrives(tmp_path, bucket):
    """So a failure half-way has still bought what it bought — and the retry
    pays only for the rest."""
    objects, uploads = bucket
    client = FakeTts(fail_on="Хоёр.")
    with pytest.raises(TtsError):
        asyncio.run(prepare(client, "p1", TRANSCRIPT, [(0.0, 4.0)], tmp_path))
    assert len(uploads) == 1
    assert objects[uploads[0]] == "mp3:Нэг.".encode()


def test_a_line_with_no_translation_is_counted_and_not_read(tmp_path, bucket):
    segments = [_seg(0, 0.0, 2.0, "Нэг."), _seg(1, 2.0, 4.0, None), _seg(2, 4.0, 5.0, None, text=" ")]
    client = FakeTts()
    paths, report = asyncio.run(prepare(client, "p1", segments, [(0.0, 5.0)], tmp_path))
    assert set(paths) == {"s0"} and report.missing == 1


# --------------------------------------------------------------------------
# A voice per speaker
# --------------------------------------------------------------------------

DIALOGUE = [
    _seg(0, 0.0, 2.0, "Нэг.", speaker="speaker_0"),
    _seg(1, 2.0, 4.0, "Хоёр.", speaker="speaker_1"),
    _seg(2, 4.0, 6.0, "Гурав."),                      # no speaker
    _seg(3, 6.0, 8.0, "Дөрөв.", speaker="speaker_2"),  # given no voice
]


def test_each_speaker_is_read_in_the_voice_they_were_given(tmp_path, bucket):
    objects, _ = bucket
    client = FakeTts()
    voices = {"speaker_0": "Anna", "speaker_1": "Bold"}
    paths, report = asyncio.run(prepare(client, "p1", DIALOGUE, [(0.0, 8.0)], tmp_path, voices=voices))

    # A line with no speaker, and a speaker given no voice: the default.
    assert client.calls == [("Anna", "Нэг."), ("Bold", "Хоёр."), ("V1", "Гурав."), ("V1", "Дөрөв.")]
    # Stored under the voice that spoke it — where the next export looks.
    assert clip_key("p1", client.config.fingerprint("Anna"), "Нэг.") in objects
    assert clip_key("p1", client.config.fingerprint("V1"), "Гурав.") in objects
    assert report.synthesized == 4


def test_the_same_words_in_two_voices_are_two_clips(tmp_path, bucket):
    segments = [_seg(0, 0.0, 1.0, "Тийм.", speaker="a"), _seg(1, 1.0, 2.0, "Тийм.", speaker="b")]
    client = FakeTts()
    paths, report = asyncio.run(
        prepare(client, "p1", segments, [(0.0, 2.0)], tmp_path, voices={"a": "Anna", "b": "Bold"})
    )
    assert client.calls == [("Anna", "Тийм."), ("Bold", "Тийм.")]
    assert paths["s0"] != paths["s1"] and report.synthesized == 2


def test_a_speaker_given_the_default_voice_shares_its_clips(tmp_path, bucket):
    """The default voice named outright is still the default voice: its
    clips are the ones already bought, not twins of them."""
    segments = [_seg(0, 0.0, 1.0, "Тийм.", speaker="a"), _seg(1, 1.0, 2.0, "Тийм.")]
    client = FakeTts()
    paths, report = asyncio.run(
        prepare(client, "p1", segments, [(0.0, 2.0)], tmp_path, voices={"a": "V1"})
    )
    assert client.calls == [("V1", "Тийм.")]
    assert paths["s0"] == paths["s1"] and (report.synthesized, report.cached) == (1, 0)


def test_a_new_voice_for_one_speaker_pays_for_that_speakers_lines_only(tmp_path, bucket):
    first = FakeTts()
    asyncio.run(prepare(first, "p1", DIALOGUE, [(0.0, 8.0)], tmp_path / "first",
                        voices={"speaker_0": "Anna", "speaker_1": "Bold"}))
    again = FakeTts()
    _, report = asyncio.run(prepare(again, "p1", DIALOGUE, [(0.0, 8.0)], tmp_path / "again",
                                    voices={"speaker_0": "Anna", "speaker_1": "Tuya"}))
    assert again.calls == [("Tuya", "Хоёр.")]
    assert (report.synthesized, report.cached) == (1, 3)


def test_the_speakers_are_listed_as_they_first_speak_with_what_they_say():
    segments = [
        _seg(0, 0.0, 1.0, text="Welcome back.", speaker="speaker_1"),
        _seg(1, 1.0, 2.0, text="  ", speaker="speaker_2"),        # no words: not speech
        _seg(2, 2.0, 3.0, text="Thanks.", speaker="speaker_0"),
        _seg(3, 3.0, 4.0, text="Nobody's line."),                 # no speaker: the default voice
        _seg(4, 4.0, 5.0, text="x" * 200, speaker="speaker_1"),
    ]
    assert speakers(segments) == [
        {"id": "speaker_1", "lines": 2, "sample": "Welcome back."},
        {"id": "speaker_0", "lines": 1, "sample": "Thanks."},
    ]
    long_first = [_seg(0, 0.0, 1.0, text="  " + "y" * 200, speaker="s")]
    assert speakers(long_first)[0]["sample"] == "y" * SAMPLE_CHARS


# --------------------------------------------------------------------------
# With a real ffmpeg
# --------------------------------------------------------------------------

def _tone(path: Path, seconds: float, freq: float = 880.0, amp: float = 0.5) -> Path:
    n = int(seconds * SAMPLE_RATE)
    samples = array("h", (int(amp * 32767 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
                          for i in range(n)))
    return write_wav(path, samples.tobytes())


def _rms(pcm: bytes, start: float, end: float) -> float:
    samples = array("h", pcm)[int(start * SAMPLE_RATE): int(end * SAMPLE_RATE)]
    return math.sqrt(sum(s * s for s in samples) / max(1, len(samples))) / 32768


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getframerate()) == (1, SAMPLE_RATE)
        return w.readframes(w.getnframes())


@needs_ffmpeg
def test_a_track_puts_each_line_where_it_was_said(tmp_path):
    segments = [_seg(0, 1.0, 2.0, "Нэг."), _seg(1, 2.5, 3.2, "Хоёр.")]
    audio = {"s0": _tone(tmp_path / "a.wav", 0.5), "s1": _tone(tmp_path / "b.wav", 0.5)}
    voice = VoiceOver(ffmpeg=Path(FFMPEG), segments=segments, audio=audio, original_volume=0.2)

    track = asyncio.run(voice.track_for(0.0, 4.0, tmp_path / "track.wav"))
    pcm = _read_wav(track)

    assert len(pcm) == 4 * SAMPLE_RATE * 2
    assert _rms(pcm, 0.1, 0.9) == 0.0
    assert _rms(pcm, 1.05, 1.45) > 0.2
    assert _rms(pcm, 1.6, 2.4) == 0.0
    assert _rms(pcm, 2.55, 2.95) > 0.2
    assert voice.report.sped_up == 0 and voice.report.cut == 0


@needs_ffmpeg
def test_a_line_too_long_for_its_place_is_hurried_and_counted(tmp_path):
    segments = [_seg(0, 0.0, 1.0, "Нэг."), _seg(1, 1.0, 2.0, "Хоёр.")]
    audio = {"s0": _tone(tmp_path / "a.wav", 1.2), "s1": _tone(tmp_path / "b.wav", 0.5)}
    voice = VoiceOver(ffmpeg=Path(FFMPEG), segments=segments, audio=audio, original_volume=0.2)

    pcm = _read_wav(asyncio.run(voice.track_for(0.0, 3.0, tmp_path / "track.wav")))

    assert voice.report.sped_up == 1
    # 1.2 s at 1.2x fits the one second before the next line is due.
    assert _rms(pcm, 1.02, 1.05) < _rms(pcm, 0.5, 0.9)


@needs_ffmpeg
def test_a_hurried_line_is_played_faster_not_cut_shorter(tmp_path):
    """Cut to the same length, a line played at its own pace would lose its
    last words; played faster, it keeps them all."""
    line = _tone(tmp_path / "line.wav", 1.2)
    hurried = asyncio.run(voiceover.decode(Path(FFMPEG), line, tempo=1.2))
    assert len(hurried) / (SAMPLE_RATE * 2) == pytest.approx(1.0, abs=0.03)


@needs_ffmpeg
def test_a_clip_with_nothing_to_say_gets_no_track(tmp_path):
    voice = VoiceOver(ffmpeg=Path(FFMPEG), segments=[_seg(0, 0.0, 1.0, "Нэг.")],
                      audio={"s0": _tone(tmp_path / "a.wav", 0.5)}, original_volume=0.2)
    assert asyncio.run(voice.track_for(5.0, 8.0, tmp_path / "t.wav")) is None


@needs_ffmpeg
def test_the_voice_is_heard_over_the_original_kept_at_its_level(tmp_path):
    """The whole mix, through the same ffmpeg call the render makes."""
    from app.export.pipeline import _cut_and_normalize_clip
    from app.timeline.models import Clip
    from app.video.ffmpeg import discover_ffmpeg
    from tests.test_export_pipeline import _Handle

    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)],
        check=True,
    )
    line = _tone(tmp_path / "line.wav", 1.0, freq=880.0, amp=0.5)
    voice_track = write_wav(tmp_path / "voice.wav", assemble([(2.0, _read_wav(line))], 4.0))

    def render(out: Path, **voice) -> bytes:
        asyncio.run(_cut_and_normalize_clip(
            discover_ffmpeg(), _Handle(),
            Clip(id="c", source_path=str(src), start=0.0, end=4.0, order=0),
            True, 320, 240, 25.0, 30, "ultrafast", "landscape", "pad", out, 0.0, 1.0, **voice,
        ))
        return subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(out),
             "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
            check=True, capture_output=True,
        ).stdout

    # The same clip without a voice is the yardstick: the same channel
    # conversions, so the only difference left is the level asked for.
    plain = render(tmp_path / "plain.mp4")
    mixed = render(tmp_path / "mixed.mp4", voice_path=voice_track, original_volume=0.2)
    # Where no line plays, the original at a fifth of its level.
    assert _rms(mixed, 0.5, 1.5) == pytest.approx(_rms(plain, 0.5, 1.5) * 0.2, rel=0.1)
    # Where the line plays, far louder than the ducked original alone.
    assert _rms(mixed, 2.2, 2.8) > 3 * _rms(mixed, 0.5, 1.5)
    # And the length is the clip's, not the voice's or the source's.
    assert len(mixed) / (SAMPLE_RATE * 2) == pytest.approx(4.0, abs=0.1)


def test_voiceover_module_names_its_ceiling_in_one_place():
    """The tempo the placement allows is the one the decode applies."""
    assert voiceover.MAX_TEMPO <= 2.0  # atempo's own ceiling for one filter
