"""The four job handlers, end to end, with R2, STT, LLM and ffmpeg faked.

MEASURED before this file existed: 223 of worker.py's 317 statements never ran
under test — the whole body of every handler. That is where the chain actually
lives, and it was the least-exercised code in the backend. Every one of the
seven defects production surfaced in a single day sat on those lines.

What these aim at is the CHAIN, not the calls. A test that asserts
`download_file was called` passes just as happily when the key it was called
with is the wrong one; the assertions here follow a value from where one
handler writes it to where the next one reads it, and check what the outside
world was actually asked for.

The four seams are faked because they are a network, a GPU-class dependency,
a paid API and a subprocess. Everything between them is real: the database,
the store, the key builders, the SRT writer, the models.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import r2
from app.dbmodels import Job, Project, User
from app.jobs import queue
from app.jobs.queue import JobHandle
from app.models import (
    Clip,
    Cut,
    KeepRange,
    Segment,
    ShortIdea,
    Suggestions,
    Transcript,
    VideoMeta,
    YoutubePlan,
)
from app.security import hash_password
from app.store import load, save
from app.video.ffmpeg import FfmpegBinaries
from tests.conftest import requires_db

pytestmark = requires_db

FFMPEG = Path("/fake/bin/ffmpeg")
FFPROBE = Path("/fake/bin/ffprobe")
PROBE = {
    "duration_sec": 120.0,
    "width": 1920,
    "height": 1080,
    "fps": 30.0,
    "has_audio": True,
    "codec": "h264",
}


# --------------------------------------------------------------------------
# The fake outside world.
# --------------------------------------------------------------------------


class FakeR2:
    """An in-memory bucket that refuses to hand back a key nobody put there.

    That refusal is the point. A real chain break — one handler writing
    `audio/<id>/audio.wav` and the next reading `audio/<id>/source.wav` —
    shows up here as a KeyError naming both, where a permissive double would
    hand over empty bytes and let the test pass.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.uploads: list[tuple[str, str | None]] = []

    def put(self, key: str, data: bytes = b"bytes") -> None:
        self.objects[key] = data

    def download_file(self, key: str, local_path: Path) -> Path:
        self.downloads.append(key)
        if key not in self.objects:
            raise KeyError(f"no object at {key!r}; the bucket holds {sorted(self.objects)}")
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(self.objects[key])
        return Path(local_path)

    def upload_file(self, local_path: Path, key: str, content_type: str | None = None) -> int:
        data = Path(local_path).read_bytes()
        self.objects[key] = data
        self.uploads.append((key, content_type))
        return len(data)


class Recorder:
    """What the faked seams were asked to do, in order."""

    def __init__(self) -> None:
        self.r2 = FakeR2()
        self.audio_extracts: list[tuple[str, Path]] = []  # (kind, destination)
        self.thumbnail_at: float | None = None
        self.transcribed: Path | None = None
        self.stt_closed = False
        self.llm_closed = False
        self.llm_built = 0
        self.rendered_clips: list[Clip] = []
        self.render_kwargs: dict = {}
        self.transcript = Transcript(
            language="mn",
            segments=[Segment(id="s1", start=0.0, end=2.0, text="Сайн байна уу.")],
            full_text="Сайн байна уу.",
        )
        self.suggestions = _suggestions()


def _suggestions(*, youtube: bool = False) -> Suggestions:
    shorts = [
        ShortIdea(
            id=f"s{i}", title=f"Short {i}", hook_text="h", hook_quote="q",
            cuts=[
                Cut(start=0.0, end=5.0, role="hook", reason="r"),
                Cut(start=10.0, end=20.0, role="payoff", reason="r"),
            ],
            caption="c", why_it_works="w",
        )
        for i in range(3)
    ]
    plans = (
        [
            YoutubePlan(
                title=f"Plan {i}", throughline="t",
                ranges=[KeepRange(start=0.0, end=600.0)], total_duration=600.0,
            )
            for i in range(3)
        ]
        if youtube
        else []
    )
    return Suggestions(shorts=shorts, youtube=plans)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Installs the doubles and hands back what they recorded."""
    from app import config, worker

    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    config.get_settings.cache_clear()

    rec = Recorder()

    monkeypatch.setattr(r2, "download_file", rec.r2.download_file)
    monkeypatch.setattr(r2, "upload_file", rec.r2.upload_file)

    monkeypatch.setattr(worker, "discover_ffmpeg", lambda: FfmpegBinaries(FFMPEG, FFPROBE))

    async def fake_probe(ffprobe, path):
        return dict(PROBE)

    async def fake_thumbnail(ffmpeg, video, out, at_sec=1.0):
        rec.thumbnail_at = at_sec
        Path(out).write_bytes(b"jpeg")

    async def fake_16k(ffmpeg, src, dest):
        rec.audio_extracts.append(("16k_mono", Path(dest)))
        Path(dest).write_bytes(b"wav16k")

    async def fake_native(ffmpeg, src, dest):
        rec.audio_extracts.append(("native", Path(dest)))
        Path(dest).write_bytes(b"wavnative")

    monkeypatch.setattr(worker, "probe_video", fake_probe)
    monkeypatch.setattr(worker, "generate_thumbnail", fake_thumbnail)
    monkeypatch.setattr(worker, "extract_audio_16k_mono_wav", fake_16k)
    monkeypatch.setattr(worker, "extract_audio_native_wav", fake_native)

    class FakeClient:
        def __init__(self, which: str) -> None:
            self.which = which

        async def aclose(self) -> None:
            if self.which == "stt":
                rec.stt_closed = True
            else:
                rec.llm_closed = True

    def build_llm(settings):
        rec.llm_built += 1
        return FakeClient("llm")

    async def fake_transcribe(client, audio_path, workdir, settings, ffmpeg, on_progress=None):
        rec.transcribed = Path(audio_path)
        if on_progress:
            await on_progress(1.0)
        return rec.transcript

    async def fake_suggest(client, transcript, duration_sec):
        return rec.suggestions

    monkeypatch.setattr(worker, "build_stt_client", lambda s: FakeClient("stt"))
    monkeypatch.setattr(worker, "build_llm_client", build_llm)
    monkeypatch.setattr(worker, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(worker, "generate_suggestions", fake_suggest)
    monkeypatch.setattr(worker, "separation_available", lambda s: False)

    async def fake_caps(ffmpeg):
        return type("Caps", (), {"xfade_transitions": frozenset({"fade"})})()

    monkeypatch.setattr(worker, "build_capabilities", fake_caps)

    async def fake_render_timeline(handle, binaries, clips, transition, **kwargs):
        rec.rendered_clips = list(clips)
        rec.render_kwargs = kwargs
        out = Path(kwargs["output_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return out

    async def fake_render_all(handle, binaries, video_path, suggestions, transition, **kwargs):
        rec.render_kwargs = kwargs
        out_dir = Path(kwargs["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for i, idea in enumerate(suggestions.shorts, start=1):
            path = out_dir / f"short_{i}.mp4"
            path.write_bytes(b"mp4")
            items.append({"kind": "short", "title": idea.title, "output_path": str(path)})
        for i, plan in enumerate(suggestions.youtube, start=1):
            path = out_dir / f"youtube_{i}.mp4"
            path.write_bytes(b"mp4")
            path.with_suffix(".srt").write_text("1\n", encoding="utf-8")
            items.append({"kind": "youtube", "title": plan.title, "output_path": str(path)})
        return items

    monkeypatch.setattr("app.export.pipeline.render_timeline", fake_render_timeline)
    monkeypatch.setattr("app.export.pipeline.render_all_ideas", fake_render_all)

    yield rec
    config.get_settings.cache_clear()


@pytest.fixture
def project(db):
    salt, digest = hash_password("pw")
    user = User(username="wowner", pw_salt=salt, pw_hash=digest)
    db.add(user)
    db.flush()
    row = Project(owner_id=user.id, name="worker chain", doc={})
    db.add(row)
    db.commit()
    return row


def _running_job(db, project_id: str | None, kind: str) -> Job:
    """A real queue row, enqueued through the real registry.

    The row has to exist: every handler reports progress, and a flush against
    a job the queue cannot find reads as "cancelled" — which is the correct
    behaviour and would otherwise make every test here look like a cancel.
    Going through `enqueue` also means priority, lane and no_retry come from
    the registry rather than from a literal that drifts away from it.
    """
    job_id = queue.enqueue(kind, project_id=project_id)
    db.expire_all()
    job = db.get(Job, job_id)
    job.state = "running"
    db.commit()
    db.refresh(job)
    return job


def _handle(db, project_id: str, kind: str = "import_video") -> JobHandle:
    job = _running_job(db, project_id, kind)
    return JobHandle(job_id=job.id, kind=kind, project_id=project_id, payload={})


def _with_video(db, project_id: str, **overrides) -> None:
    """Puts an imported video on the project, as handle_import_video would."""
    project = load(db, project_id)
    project.video = VideoMeta(
        source_key=f"sources/{project_id}/source.mp4",
        duration_sec=PROBE["duration_sec"], width=PROBE["width"], height=PROBE["height"],
        fps=PROBE["fps"], has_audio=True, codec="h264",
        thumbnail_key=r2.thumbnail_key(project_id),
        audio_key=overrides.pop("audio_key", r2.audio_key(project_id, "audio.wav")),
        **overrides,
    )
    save(db, project)
    db.commit()


# ==========================================================================
# import_video
# ==========================================================================


def test_import_hands_transcribe_a_key_it_can_actually_read(world, project, db):
    """The chain link between the two handlers.

    Import extracts the speech track once and records where it put it;
    transcribe reads that key instead of pulling the whole source down again.
    If the two ever name it differently the optimisation silently reverses —
    every re-run downloads 200 MB again — and nothing fails. Here the fake
    bucket refuses a key that was never uploaded, so the break is a failure.
    """
    from app import worker

    project.video_key = f"sources/{project.id}/source.mp4"
    db.commit()
    world.r2.put(project.video_key)

    asyncio.run(worker.handle_import_video(_handle(db, project.id)))

    db.expire_all()
    stored = load(db, project.id)
    world.r2.download_file(stored.video.audio_key, Path("/tmp/roundtrip.wav"))

    assert stored.video.audio_key in world.r2.objects
    assert db.get(Project, project.id).audio_key == stored.video.audio_key


def test_import_records_the_thumbnail_in_both_places(world, project, db):
    """The document is what a render reads; the row is what a project list
    reads. One written without the other is a thumbnail that exists and never
    appears, or a card pointing at nothing."""
    from app import worker

    project.video_key = f"sources/{project.id}/source.mp4"
    db.commit()
    world.r2.put(project.video_key)

    asyncio.run(worker.handle_import_video(_handle(db, project.id)))

    db.expire_all()
    row = db.get(Project, project.id)
    assert row.thumbnail_key == load(db, project.id).video.thumbnail_key
    assert row.thumbnail_key in world.r2.objects


def test_the_thumbnail_is_taken_from_the_middle(world, project, db):
    """An opening fade or a black slate makes a thumbnail that identifies
    nothing, so the frame comes from half way in."""
    from app import worker

    project.video_key = f"sources/{project.id}/source.mp4"
    db.commit()
    world.r2.put(project.video_key)

    asyncio.run(worker.handle_import_video(_handle(db, project.id)))

    assert world.thumbnail_at == pytest.approx(PROBE["duration_sec"] / 2)


def test_a_silent_video_imports_without_an_audio_key(world, project, db, monkeypatch):
    """No audio track is an ordinary video, not a failure — the import still
    has to produce metadata and a thumbnail."""
    from app import worker

    async def silent(ffprobe, path):
        return {**PROBE, "has_audio": False}

    monkeypatch.setattr(worker, "probe_video", silent)
    project.video_key = f"sources/{project.id}/source.mp4"
    db.commit()
    world.r2.put(project.video_key)

    result = asyncio.run(worker.handle_import_video(_handle(db, project.id)))

    db.expire_all()
    assert result["has_audio"] is False
    assert load(db, project.id).video.audio_key == ""
    assert world.audio_extracts == []
    assert db.get(Project, project.id).thumbnail_key


def test_import_without_an_upload_says_so_before_touching_ffmpeg(world, project, db):
    """The API records the key when the browser's upload finishes. Reaching
    here without one means the upload never landed, and the useful error names
    that rather than surfacing as a download of nothing."""
    from app import worker

    with pytest.raises(RuntimeError, match="no uploaded source video"):
        asyncio.run(worker.handle_import_video(_handle(db, project.id)))
    assert world.r2.downloads == []


# ==========================================================================
# transcribe
# ==========================================================================


def test_transcribe_takes_the_stored_audio_and_never_the_video(world, project, db):
    """~32 MB instead of the whole source, and no second ffmpeg pass. The
    saving only exists if the video is genuinely not fetched."""
    from app import worker

    _with_video(db, project.id)
    world.r2.put(r2.audio_key(project.id, "audio.wav"), b"wav")
    world.r2.put(f"sources/{project.id}/source.mp4")

    asyncio.run(worker.handle_transcribe(_handle(db, project.id, "transcribe")))

    assert world.r2.downloads == [r2.audio_key(project.id, "audio.wav")]
    assert world.audio_extracts == []


def test_separation_forces_the_video_and_the_native_rate(world, project, db, monkeypatch):
    """Demucs degrades on audio already narrowed to the 16 kHz mono STT
    contract, so with separation on the stored shortcut must be skipped and
    the extraction must be the native-rate one — not merely 'some extraction'.
    """
    from app import worker

    monkeypatch.setattr(worker, "separation_available", lambda s: True)
    _with_video(db, project.id)
    world.r2.put(r2.audio_key(project.id, "audio.wav"), b"wav")
    world.r2.put(f"sources/{project.id}/source.mp4")

    asyncio.run(worker.handle_transcribe(_handle(db, project.id, "transcribe")))

    assert world.r2.downloads == [f"sources/{project.id}/source.mp4"]
    assert [kind for kind, _ in world.audio_extracts] == ["native"]
    assert world.transcribed == world.audio_extracts[0][1]


def test_a_project_imported_before_the_audio_shortcut_still_transcribes(world, project, db):
    """Projects imported before import_video extracted audio have no stored
    key. They fall back to the video and the 16 kHz extraction."""
    from app import worker

    _with_video(db, project.id, audio_key="")
    world.r2.put(f"sources/{project.id}/source.mp4")

    asyncio.run(worker.handle_transcribe(_handle(db, project.id, "transcribe")))

    assert world.r2.downloads == [f"sources/{project.id}/source.mp4"]
    assert [kind for kind, _ in world.audio_extracts] == ["16k_mono"]


def test_the_uploaded_srt_is_the_transcript_that_was_stored(world, project, db):
    """Two artefacts, one source. A subtitle file that disagrees with the
    transcript the editor shows is worse than no subtitle file."""
    from app import worker
    from app.subtitle.srt import segments_to_srt

    _with_video(db, project.id)
    world.r2.put(r2.audio_key(project.id, "audio.wav"), b"wav")

    result = asyncio.run(worker.handle_transcribe(_handle(db, project.id, "transcribe")))

    db.expire_all()
    stored = load(db, project.id).transcript
    assert result["srt_key"]
    assert world.r2.objects[result["srt_key"]].decode("utf-8") == segments_to_srt(stored.segments)
    assert result["segments"] == len(stored.segments)


def test_an_empty_transcript_uploads_no_subtitle_file(world, project, db):
    """An empty .srt sitting in the bucket reads as "subtitles exist" to
    everything downstream. Absent is the honest answer."""
    from app import worker

    world.transcript = Transcript(language="mn", segments=[], full_text="")
    _with_video(db, project.id)
    world.r2.put(r2.audio_key(project.id, "audio.wav"), b"wav")

    result = asyncio.run(worker.handle_transcribe(_handle(db, project.id, "transcribe")))

    assert result["srt_key"] is None
    assert world.r2.uploads == []


def test_transcribing_a_silent_video_is_refused(world, project, db):
    from app import worker

    project_doc = load(db, project.id)
    project_doc.video = VideoMeta(
        source_key=f"sources/{project.id}/source.mp4", duration_sec=10.0,
        width=1920, height=1080, fps=30.0, has_audio=False, codec="h264",
    )
    save(db, project_doc)
    db.commit()

    with pytest.raises(RuntimeError, match="no audio track"):
        asyncio.run(worker.handle_transcribe(_handle(db, project.id, "transcribe")))


def test_the_stt_client_is_closed_even_when_transcription_fails(world, project, db, monkeypatch):
    """A leaked httpx client holds a connection open for the life of the
    worker, and a transcribe that fails is the common case, not the rare one.
    """
    from app import worker

    async def boom(*a, **k):
        raise RuntimeError("provider said no")

    monkeypatch.setattr(worker, "transcribe_audio", boom)
    _with_video(db, project.id)
    world.r2.put(r2.audio_key(project.id, "audio.wav"), b"wav")

    with pytest.raises(RuntimeError, match="provider said no"):
        asyncio.run(worker.handle_transcribe(_handle(db, project.id, "transcribe")))
    assert world.stt_closed


# ==========================================================================
# suggest
# ==========================================================================


def test_suggest_refuses_before_it_can_spend_money(world, project, db):
    """suggest is billed per attempt and is not retried. A project with no
    transcript must be turned away BEFORE a client exists, not after a call
    that charges for an answer about nothing."""
    from app import worker

    _with_video(db, project.id)

    with pytest.raises(RuntimeError, match="Transcribe the video"):
        asyncio.run(worker.handle_suggest(_handle(db, project.id, "suggest")))
    assert world.llm_built == 0


def test_suggestions_are_stored_and_counted(world, project, db):
    from app import worker

    _with_video(db, project.id)
    project_doc = load(db, project.id)
    project_doc.transcript = world.transcript
    save(db, project_doc)
    db.commit()

    result = asyncio.run(worker.handle_suggest(_handle(db, project.id, "suggest")))

    db.expire_all()
    stored = load(db, project.id).suggestions
    assert result == {"shorts": 3, "youtube": 0}
    assert [s.title for s in stored.shorts] == ["Short 0", "Short 1", "Short 2"]
    assert world.llm_closed


# ==========================================================================
# export / export_all
# ==========================================================================


def _ready_to_render(db, project_id: str, *, clips: bool = True, suggestions=None) -> None:
    _with_video(db, project_id)
    project = load(db, project_id)
    project.transcript = Transcript(
        language="mn",
        segments=[Segment(id="s1", start=0.0, end=2.0, text="Сайн байна уу.")],
        full_text="Сайн байна уу.",
    )
    if clips:
        project.clips = [
            Clip(id="c1", source_path="C:/Users/desktop-era/video.mp4", start=0.0, end=5.0, order=0)
        ]
    if suggestions is not None:
        project.suggestions = suggestions
    save(db, project)
    db.commit()


def test_the_render_reads_the_copy_this_worker_downloaded(world, project, db):
    """The strongest link in the export chain.

    Stored clips carry the desktop era's absolute source path. On a container
    that path is meaningless, and the renderer would either fail on a missing
    file or — worse, on a box where something happens to sit there — encode
    the wrong video. Every clip must be rewritten to the scratch copy.
    """
    from app import worker

    _ready_to_render(db, project.id)
    world.r2.put(f"sources/{project.id}/source.mp4")

    asyncio.run(worker.handle_export(_handle(db, project.id, "export")))

    assert world.rendered_clips, "the renderer was never called"
    paths = {c.source_path for c in world.rendered_clips}
    assert len(paths) == 1
    downloaded = Path(paths.pop())
    assert downloaded.is_file()
    assert "desktop-era" not in str(downloaded)


def test_a_logo_nobody_uploaded_costs_neither_a_download_nor_the_render(world, project, db):
    """A producer discovering at render time that a missing title card cost
    them a 20-minute encode is the outcome this avoids: the mark is a
    warning, never a failure."""
    from app import worker

    _ready_to_render(db, project.id)
    doc = load(db, project.id)
    doc.export.logo.enabled = True
    doc.export.use_intro = True
    save(db, doc)
    db.commit()
    world.r2.put(f"sources/{project.id}/source.mp4")

    result = asyncio.run(worker.handle_export(_handle(db, project.id, "export")))

    assert world.render_kwargs["logo"] is None
    assert world.render_kwargs["intro_path"] is None
    assert len(result["outputs"]) == 1


def test_a_logo_the_project_turned_off_is_never_fetched(world, project, db):
    """Both the global mark and the per-project choice have to be true before
    a byte is fetched."""
    from app import brand, worker

    _ready_to_render(db, project.id)
    brand.set_asset(db, "logo", "brand/logo.png")
    db.commit()
    world.r2.put("brand/logo.png")
    world.r2.put(f"sources/{project.id}/source.mp4")

    asyncio.run(worker.handle_export(_handle(db, project.id, "export")))

    assert "brand/logo.png" not in world.r2.downloads
    assert world.render_kwargs["logo"] is None


def test_an_uploaded_logo_reaches_the_render_with_the_projects_placement(world, project, db):
    from app import brand, worker

    _ready_to_render(db, project.id)
    doc = load(db, project.id)
    doc.export.logo.enabled = True
    doc.export.logo.position = "bottom_left"
    save(db, doc)
    brand.set_asset(db, "logo", "brand/logo.png")
    db.commit()
    world.r2.put("brand/logo.png", b"png")
    world.r2.put(f"sources/{project.id}/source.mp4")

    asyncio.run(worker.handle_export(_handle(db, project.id, "export")))

    assert "brand/logo.png" in world.r2.downloads
    assert world.render_kwargs["logo"].position == "bottom_left"
    assert Path(world.render_kwargs["logo_path"]).is_file()


def test_exporting_an_empty_timeline_is_refused(world, project, db):
    from app import worker

    _ready_to_render(db, project.id, clips=False)
    world.r2.put(f"sources/{project.id}/source.mp4")

    with pytest.raises(RuntimeError, match="no clips"):
        asyncio.run(worker.handle_export(_handle(db, project.id, "export")))


def test_export_all_numbers_each_kind_separately_and_keeps_its_srt(world, project, db):
    """Six outputs land as short_1..3 and youtube_1..3, not 1..6. The index is
    per kind because that is what the key builder promises, and an .srt
    written beside a render has to follow its video's key or it is orphaned.
    """
    from app import worker
    from app.dbmodels import Output

    _ready_to_render(db, project.id, clips=False, suggestions=_suggestions(youtube=True))
    world.r2.put(f"sources/{project.id}/source.mp4")

    result = asyncio.run(worker.handle_export_all(_handle(db, project.id, "export_all")))

    keys = [item["key"] for item in result["outputs"]]
    assert keys == [
        r2.output_key(project.id, "short", 1, "mp4"),
        r2.output_key(project.id, "short", 2, "mp4"),
        r2.output_key(project.id, "short", 3, "mp4"),
        r2.output_key(project.id, "youtube", 1, "mp4"),
        r2.output_key(project.id, "youtube", 2, "mp4"),
        r2.output_key(project.id, "youtube", 3, "mp4"),
    ]
    for item in result["outputs"]:
        if item["srt_key"]:
            assert item["srt_key"] == item["key"].rsplit(".", 1)[0] + ".srt"
            assert item["srt_key"] in world.r2.objects

    db.expire_all()
    rows = db.query(Output).filter(Output.project_id == project.id).all()
    assert {row.r2_key for row in rows} == set(keys)
    assert all(row.size_bytes > 0 for row in rows)


def test_export_all_without_suggestions_is_refused(world, project, db):
    from app import worker

    _ready_to_render(db, project.id, clips=False)
    world.r2.put(f"sources/{project.id}/source.mp4")

    with pytest.raises(RuntimeError, match="no suggestions"):
        asyncio.run(worker.handle_export_all(_handle(db, project.id, "export_all")))


def test_the_transcript_reaches_the_render_for_subtitles(world, project, db):
    """burn_subtitles and write_srt are both on by default, and neither can
    do anything without the segments. Passing None here produces a silent
    export with no subtitles at all."""
    from app import worker

    _ready_to_render(db, project.id)
    world.r2.put(f"sources/{project.id}/source.mp4")

    asyncio.run(worker.handle_export(_handle(db, project.id, "export")))

    assert world.render_kwargs["transcript_segments"]
    assert world.render_kwargs["transcript_segments"][0].text == "Сайн байна уу."


# ==========================================================================
# _run_job — where a handler's result becomes the job's record.
# ==========================================================================




def test_a_finished_job_records_the_handler_result(world, project, db):
    from app import worker

    _with_video(db, project.id)
    project_doc = load(db, project.id)
    project_doc.transcript = world.transcript
    save(db, project_doc)
    db.commit()
    job = _running_job(db, project.id, "suggest")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    done = db.get(Job, job.id)
    assert done.state == "done"
    assert done.result["output"]["shorts"] == 3
    assert "elapsed_sec" in done.result["output"]


def test_a_job_that_spent_nothing_carries_no_llm_field(world, project, db):
    """An absent field says "no paid call", which a zero would not."""
    from app import worker

    project.video_key = f"sources/{project.id}/source.mp4"
    db.commit()
    world.r2.put(project.video_key)
    job = _running_job(db, project.id, "import_video")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    assert "llm" not in db.get(Job, job.id).result["output"]


def test_a_billed_kind_that_fails_is_not_queued_again(world, project, db, monkeypatch):
    """One attempt is one bill. suggest and transcribe carry no_retry, so a
    failure has to end as failed — a requeue charges again for work the first
    attempt may already have completed remotely."""
    from app import worker

    async def boom(handle):
        raise RuntimeError("the model refused")

    monkeypatch.setitem(worker.HANDLERS, "suggest", boom)
    job = _running_job(db, project.id, "suggest")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    failed = db.get(Job, job.id)
    assert failed.state == "failed"
    assert "the model refused" in failed.error


def test_a_retryable_kind_goes_back_to_the_queue(world, project, db, monkeypatch):
    from app import worker

    async def boom(handle):
        raise RuntimeError("ffmpeg died")

    monkeypatch.setitem(worker.HANDLERS, "export", boom)
    job = _running_job(db, project.id, "export")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    assert db.get(Job, job.id).state == "queued"


def test_a_deleted_project_does_not_take_the_worker_down(world, project, db):
    """A project deleted between claim and run must end the JOB, never the
    loop. Worth stating what actually happens: jobs cascade with their
    project, so by the time the handler raises ProjectNotFound the row it
    would be marked on is already gone and `finish` is a no-op. The branch
    still earns its place — it is what keeps the exception from reaching the
    loop — and this is the observable part of it.
    """
    from app import worker

    job = _running_job(db, project.id, "suggest")
    job_id = job.id
    db.delete(db.get(Project, project.id))
    db.commit()

    asyncio.run(worker._run_job(job))  # must not raise

    db.expire_all()
    assert db.get(Job, job_id) is None


def test_the_scratch_directory_is_cleared_after_a_failure(world, project, db, monkeypatch):
    """A worker that leaks a job's scratch on every failure fills the disk,
    and a full disk stops every OTHER job too."""
    from app import worker
    from app.utils.paths import job_workdir

    async def boom(handle):
        job_workdir(handle.job_id).joinpath("half-a-render.mp4").write_bytes(b"x" * 1024)
        raise RuntimeError("died mid-render")

    monkeypatch.setitem(worker.HANDLERS, "export", boom)
    job = _running_job(db, project.id, "export")

    asyncio.run(worker._run_job(job))

    assert not job_workdir(job.id).exists() or not list(job_workdir(job.id).iterdir())


def test_a_job_that_spent_money_reports_what_it_cost(world, project, db, monkeypatch):
    """The other half of the rule above. The meter sits at the one gate every
    paid call goes through, so a handler that spends has to surface it without
    the handler itself knowing about the meter."""
    from app import worker
    from app.ai import usage as llm_usage

    async def paid(client, transcript, duration_sec):
        llm_usage.record(model="test/model", prompt=1200, completion=800, cost=0.0042)
        return world.suggestions

    monkeypatch.setattr(worker, "generate_suggestions", paid)
    _with_video(db, project.id)
    doc = load(db, project.id)
    doc.transcript = world.transcript
    save(db, doc)
    db.commit()
    job = _running_job(db, project.id, "suggest")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    spend = db.get(Job, job.id).result["output"]["llm"]
    assert spend["calls"] == 1
    assert spend["cost_usd"] == 0.0042
    assert spend["models"] == ["test/model"]


def test_a_cancelled_job_ends_as_cancelled_not_failed(world, project, db, monkeypatch):
    """A producer who pressed cancel must see "cancelled". Recorded as failed
    it reads as a bug in the render, and the retry logic treats it as one."""
    from app import worker

    async def notices_the_cancel(handle):
        handle.cancel_requested = True
        return {"outputs": []}

    monkeypatch.setitem(worker.HANDLERS, "export", notices_the_cancel)
    job = _running_job(db, project.id, "export")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    assert db.get(Job, job.id).state == "canceled"


def test_a_handler_raising_JobCancelled_ends_as_cancelled(world, project, db, monkeypatch):
    """The other way a cancel arrives: mid-call, through raise_if_cancelled."""
    from app import worker
    from app.jobs.queue import JobCancelled

    async def stopped(handle):
        raise JobCancelled()

    monkeypatch.setitem(worker.HANDLERS, "export", stopped)
    job = _running_job(db, project.id, "export")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    assert db.get(Job, job.id).state == "canceled"


def test_a_job_with_no_room_on_disk_is_deferred_before_it_starts(world, project, db, monkeypatch):
    """Checked up front rather than discovered halfway through as [Errno 28],
    which is the version that leaves a half-written file behind."""
    from app import worker
    from app.utils.paths import OutOfSpace

    started = []

    async def should_not_run(handle):
        started.append(handle.job_id)
        return {}

    def no_room(need):
        raise OutOfSpace("needs 8.0 GB, 1.2 GB free")

    monkeypatch.setitem(worker.HANDLERS, "export", should_not_run)
    monkeypatch.setattr(worker, "ensure_free", no_room)
    job = _running_job(db, project.id, "export")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    failed = db.get(Job, job.id)
    assert failed.state == "failed"
    assert "1.2 GB free" in failed.error
    assert started == []


def test_a_job_without_a_project_names_that_rather_than_crashing(world, db):
    """Every handler needs a project. A job that lost its link has to say so
    in its own error, not fail somewhere deeper with a None."""
    from app import worker

    job = _running_job(db, None, "suggest")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    failed = db.get(Job, job.id)
    assert failed.state == "failed"
    assert "has no project" in failed.error


def test_a_missing_ffmpeg_is_named_as_the_image_being_wrong(world, project, db, monkeypatch):
    """Not "export failed": the fix is a rebuild, and the error has to point
    at that rather than at the video."""
    from app import worker
    from app.video.ffmpeg import FfmpegBinaries

    monkeypatch.setattr(worker, "discover_ffmpeg", lambda: FfmpegBinaries(None, None))

    with pytest.raises(RuntimeError, match="FFmpeg is not installed"):
        asyncio.run(worker.handle_import_video(_handle(db, project.id)))


@pytest.mark.parametrize(
    "handler", ["handle_transcribe", "handle_suggest", "handle_export"]
)
def test_every_handler_that_needs_a_video_says_so(world, project, db, handler):
    """A project with no import yet reaches three different handlers, and each
    has to turn it away itself — none of them can assume an earlier one ran."""
    from app import worker

    kind = {"handle_transcribe": "transcribe", "handle_suggest": "suggest"}.get(handler, "export")
    with pytest.raises(RuntimeError, match="no video"):
        asyncio.run(getattr(worker, handler)(_handle(db, project.id, kind)))


def test_an_uploaded_intro_reaches_the_render(world, project, db):
    """The missing-asset path is a warning; this is the other side of it —
    an intro that exists has to be fetched and handed over."""
    from app import brand, worker

    _ready_to_render(db, project.id)
    doc = load(db, project.id)
    doc.export.use_intro = True
    doc.export.use_outro = True
    save(db, doc)
    brand.set_asset(db, "intro", "brand/intro.mp4")
    db.commit()
    world.r2.put("brand/intro.mp4", b"mp4")
    world.r2.put(f"sources/{project.id}/source.mp4")

    asyncio.run(worker.handle_export(_handle(db, project.id, "export")))

    assert "brand/intro.mp4" in world.r2.downloads
    assert Path(world.render_kwargs["intro_path"]).is_file()
    assert world.render_kwargs["outro_path"] is None


def test_a_render_that_produced_no_file_creates_no_output_row(world, project, db, monkeypatch):
    """An Output row pointing at a key nobody uploaded is a download button
    that 404s. Skipping is right; recording it would be worse than the gap."""
    from app import worker
    from app.dbmodels import Output

    async def renders_nothing(handle, binaries, video_path, suggestions, transition, **kwargs):
        never_written = Path(kwargs["output_dir"]) / "gone.mp4"
        return [{"kind": "short", "title": "vanished", "output_path": str(never_written)}]

    monkeypatch.setattr("app.export.pipeline.render_all_ideas", renders_nothing)
    _ready_to_render(db, project.id, clips=False, suggestions=_suggestions())
    world.r2.put(f"sources/{project.id}/source.mp4")

    result = asyncio.run(worker.handle_export_all(_handle(db, project.id, "export_all")))

    db.expire_all()
    assert result["outputs"] == []
    assert db.query(Output).filter(Output.project_id == project.id).count() == 0
