"""Every read of a file a person supplied is confined (audit SEC-5).

The attack, reduced to what this machine's ffmpeg does: an upload named
`source.mp4` that is a two-line ffconcat text. ffmpeg sniffs the content,
picks the concat demuxer, and reads the file it names from the same
directory — ffprobe reports that file's h264 and aac streams as the
upload's. Each test below runs the real binary on one such file through one
entry point, and on a genuine file of the same kind to show the guard
refuses only what it should.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import brand, schemas
from app.video import inputs
from tests.conftest import requires_db

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(FFMPEG is None or FFPROBE is None, reason="ffmpeg is not installed")


PLAYLIST = "ffconcat version 1.0\nfile secret.mp4\nduration 1.0\n"


def _run(*args: str) -> None:
    subprocess.run([FFMPEG, "-v", "error", "-y", *args], check=True)


@pytest.fixture
def media(tmp_path: Path) -> Path:
    """A real 1 s video beside a "video" that is a playlist naming it."""
    _run("-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=1",
         "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "aac", "-shortest", str(tmp_path / "secret.mp4"))
    # `duration` makes the playlist a fully readable "video": probed, seeked
    # and thumbnailed without complaint when nothing refuses it.
    (tmp_path / "source.mp4").write_text(PLAYLIST)
    return tmp_path


def _binaries():
    from app.video.ffmpeg import FfmpegBinaries

    return FfmpegBinaries(ffmpeg=Path(FFMPEG), ffprobe=Path(FFPROBE))


def test_the_attack_is_real_without_the_guard(media):
    """Not a strawman: unguarded, ffprobe reads the sibling's streams and a
    thumbnail is cut from its frames."""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
         str(media / "source.mp4")],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert out == ["h264", "aac"]
    _run("-ss", "0.5", "-i", str(media / "source.mp4"), "-frames:v", "1", str(media / "leak.jpg"))
    assert (media / "leak.jpg").stat().st_size > 0


@pytest.mark.asyncio
async def test_probe_refuses_a_playlist_posing_as_a_video(media):
    from app.video.probe import ProbeError, probe_video

    assert (await probe_video(Path(FFPROBE), media / "secret.mp4"))["has_audio"] is True
    with pytest.raises(ProbeError):
        await probe_video(Path(FFPROBE), media / "source.mp4")


@pytest.mark.asyncio
async def test_thumbnail_refuses_it(media):
    from app.video.probe import ProbeError, generate_thumbnail

    await generate_thumbnail(Path(FFMPEG), media / "secret.mp4", media / "ok.jpg", at_sec=0.5)
    with pytest.raises(ProbeError):
        await generate_thumbnail(Path(FFMPEG), media / "source.mp4", media / "bad.jpg", at_sec=0.5)
    assert not (media / "bad.jpg").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("extract", ["extract_audio_16k_mono_wav", "extract_audio_native_wav"])
async def test_audio_extraction_refuses_it(media, extract):
    from app.video import audio

    run = getattr(audio, extract)
    await run(Path(FFMPEG), media / "secret.mp4", media / "ok.wav")
    with pytest.raises(audio.AudioExtractionError):
        await run(Path(FFMPEG), media / "source.mp4", media / "bad.wav")


@pytest.mark.asyncio
async def test_the_export_cut_refuses_it(media):
    from app.export import pipeline
    from app.timeline.models import Clip
    from app.video.ffmpeg import FfmpegFailed

    class Handle:
        job_id = "job-sec5"
        cancel_requested = False

        async def set_progress(self, *a, **k) -> None:
            pass

        def raise_if_cancelled(self) -> None:
            pass

        def set_cancel_hook(self, hook) -> None:
            pass

    async def cut(name: str) -> None:
        clip = Clip(id="c", source_path=str(media / name), start=0.0, end=0.8, order=0)
        await pipeline._cut_and_normalize_clip(
            _binaries(), Handle(), clip, True, 64, 64, 10.0, 30, "ultrafast", "landscape", "pad",
            media / f"cut-{name}", 0.0, 1.0,
        )

    await cut("secret.mp4")
    with pytest.raises(FfmpegFailed):
        await cut("source.mp4")


@pytest.mark.asyncio
async def test_the_dub_bed_read_refuses_it(media):
    from app.audio import dub_bed

    w = dub_bed.window(0.0, 0.5, pad=0.0)
    assert await dub_bed.read_range(Path(FFMPEG), media / "secret.mp4", w)
    with pytest.raises(dub_bed.BedError):
        await dub_bed.read_range(Path(FFMPEG), media / "source.mp4", w)


@pytest.mark.asyncio
async def test_a_speech_clip_is_read_as_speech_only(media):
    """The TTS provider's mp3: a reply from outside, opened as mp3 or wav."""
    from app.tts import voiceover

    _run("-f", "lavfi", "-i", "sine=duration=0.5", str(media / "voice.mp3"))
    (media / "evil.mp3").write_text(PLAYLIST)
    assert await voiceover.decode(Path(FFMPEG), media / "voice.mp3")
    with pytest.raises(voiceover.VoiceOverError):
        await voiceover.decode(Path(FFMPEG), media / "evil.mp3")


def test_the_logo_is_read_as_an_image_only(media):
    """The overlay pass's arguments, run for real with each logo."""
    from app.export.overlay import build_burn_args

    _run("-f", "lavfi", "-i", "color=red:size=16x16", "-frames:v", "1", str(media / "logo.png"))
    (media / "evil.png").write_text(PLAYLIST)

    def burn(logo: str) -> int:
        args = build_burn_args(
            str(media / "secret.mp4"), str(media / logo), "[0:v][1:v]overlay[v]",
            encoder_args=["-c:v", "libx264", "-preset", "ultrafast"], audio_args=["-c:a", "copy"],
            out_path=str(media / f"burn-{logo}.mp4"),
        )
        return subprocess.run([FFMPEG, "-v", "error", "-y", *args]).returncode

    assert burn("logo.png") == 0
    assert burn("evil.png") != 0


# --- the lists let through everything the app accepts ----------------------

def _video_args(ext: str) -> list[str]:
    codecs = {".webm": ["-c:v", "libvpx", "-c:a", "libvorbis"], ".avi": ["-c:v", "mpeg4", "-c:a", "mp3"]}
    return codecs.get(ext, ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"])


@pytest.mark.parametrize(
    "ext", sorted(schemas.UPLOAD_EXTENSIONS | set(brand.CLIP_CONTENT_TYPES.values()))
)
def test_every_accepted_video_container_opens_under_the_guard(tmp_path, ext):
    path = tmp_path / f"in{ext}"
    fmt = ["-f", "mp4"] if ext == ".m4v" else []
    _run("-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=0.5", "-f", "lavfi",
         "-i", "sine=duration=0.5", *_video_args(ext), "-shortest", *fmt, str(path))
    probe = subprocess.run(
        [FFPROBE, "-v", "error", *inputs.guard(inputs.VIDEO_FORMATS), "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert "video" in probe.stdout.split()


@pytest.mark.parametrize("ext", sorted(brand.LOGO_CONTENT_TYPES.values()))
def test_every_accepted_logo_opens_under_the_guard(tmp_path, ext):
    path = tmp_path / f"logo{ext}"
    _run("-f", "lavfi", "-i", "color=red:size=16x16", "-frames:v", "1", str(path))
    # Through ffmpeg's -i, as the overlay opens it — not ffprobe's sniffing.
    decoded = subprocess.run(
        [FFMPEG, "-v", "error", *inputs.untrusted(path, inputs.IMAGE_FORMATS), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    assert decoded.returncode == 0, decoded.stderr


def test_a_url_is_refused_by_protocol_not_just_by_format(tmp_path):
    """The second list: even a container the format list allows is not
    fetched from anywhere but a local file."""
    probe = subprocess.run(
        [FFPROBE, "-v", "error", *inputs.guard(), "http://127.0.0.1:9/x.mp4"],
        capture_output=True, text=True, timeout=30,
    )
    assert probe.returncode != 0
    assert "not on whitelist" in probe.stderr


# --- what the API says about the ffmpeg build ------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app
    from app.video.capabilities import Capabilities

    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    get_settings.cache_clear()
    app.state.capabilities = Capabilities(
        ffmpeg_available=True, ffmpeg_version="6.1.1-3ubuntu5", xfade_transitions=["fade"]
    )
    app.state.ffmpeg = None
    yield TestClient(app)
    get_settings.cache_clear()


def test_health_does_not_publish_the_ffmpeg_build(client):
    body = client.get("/health").json()
    assert body["ffmpeg"] is True
    assert "6.1" not in str(body)


@requires_db
def test_capabilities_need_a_login(client):
    assert client.get("/capabilities").status_code == 401
