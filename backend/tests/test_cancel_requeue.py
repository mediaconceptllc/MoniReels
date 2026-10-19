"""A cancel stops paid work at the next request (REL-7), and a failed attempt
goes back to the queue as the same job, after a backoff, with its history
kept (ARCH-5).

What this pins:

  * every paid gate — OpenRouter, duudlaga, ElevenLabs STT and TTS — refuses
    to send once the job is canceled; the measured failure was a transcription
    canceled at chunk 7 that still sent and paid for all 40;
  * what came back before the stop is kept: transcription chunks for the next
    run, translated lines on the transcript;
  * a stage that swallows its own failures does not swallow a cancel;
  * `requeue` keeps the dedupe key, sets no `finished_at`, backs off, and
    records the attempt where the next one cannot overwrite it.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import wave

import httpx
import pytest

from app.ai import translate as translate_mod
from app.ai.openrouter_client import OpenRouterClient, OpenRouterConfig
from app.dbmodels import Job, Project, SttChunk, User
from app.jobs import cancel, queue
from app.jobs.cancel import JobCancelled
from app.models import Segment, Transcript
from app.security import hash_password
from app.stt.chunk_cache import ChunkCache
from app.stt.duudlaga_client import DuudlagaClient, DuudlagaConfig
from app.stt.elevenlabs_client import ElevenLabsSttClient, ElevenLabsSttConfig
from app.tts.elevenlabs import ElevenLabsTts, VoiceConfig
from tests.conftest import requires_db

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


@pytest.fixture(autouse=True)
def _unwatched():
    """No cancel check left behind: a sync test's `watch()` would otherwise
    stay set for every test after it in this thread."""
    token = cancel._check.set(None)
    yield
    cancel._check.reset(token)


class Switch:
    """A job's cancel flag, flipped by the test (or by a fake provider after
    N requests) — and the check every gate runs."""

    def __init__(self) -> None:
        self.on = False

    def check(self) -> None:
        if self.on:
            raise JobCancelled()


def _wav(path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def _chunked(tmp_path, monkeypatch, handler, extracted: list | None = None, **cfg):
    """A whole-file duudlaga transcribe with ffmpeg stubbed out: 60 seconds
    with a pause every 10, sent one chunk at a time so the order of the
    requests is the order of the chunks. Each chunk's bytes name its span,
    so every chunk has its own digest."""
    import app.stt.duudlaga_client as mod

    audio = tmp_path / "audio.wav"
    _wav(audio, seconds=60.0)
    monkeypatch.setattr(mod, "_require_ffmpeg", lambda: tmp_path / "ffmpeg")

    async def _silences(ffmpeg, path):
        return [(t - 0.2, t + 0.2) for t in (10.0, 20.0, 30.0, 40.0, 50.0)]

    async def _extract(ffmpeg, src, out, start, end):
        if extracted is not None:
            extracted.append((start, end))
        out.write_bytes(f"chunk {start:.2f}-{end:.2f}".encode())

    monkeypatch.setattr(mod, "detect_silences", _silences)
    monkeypatch.setattr(mod, "extract_chunk", _extract)
    config = {
        "base_url": "https://api.duudlaga.dev/v1",
        "api_key": "dk_live_test",
        "max_audio_sec": 12.0,
        "upload_bitrate": "",
        "concurrency": 1,
    }
    config.update(cfg)
    client = DuudlagaClient(
        DuudlagaConfig(**config),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return client, audio


def _numbered():
    """A provider that answers chunk N with "N." and counts what it was sent."""
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(len(sent) + 1)
        return httpx.Response(200, json={"text": f"{len(sent)}."})

    return sent, handler


@pytest.mark.asyncio
async def test_a_canceled_transcription_stops_sending_chunks_and_says_how_far_it_got(
    tmp_path, monkeypatch
):
    """MEASURED before this: a transcription canceled at chunk 7 sent and paid
    for all 40, and the transcript was then thrown away."""
    switch = Switch()
    sent, numbered = _numbered()

    def handler(request):
        response = numbered(request)
        if len(sent) == 2:
            switch.on = True  # the producer pressed cancel while chunk 2 ran
        return response

    extracted: list = []
    client, audio = _chunked(tmp_path, monkeypatch, handler, extracted)
    cancel.watch(switch.check)

    with pytest.raises(JobCancelled) as stopped:
        await client.transcribe(audio)

    assert len(sent) == 2
    # The waiting chunks stop before their cut, not one ffmpeg run later.
    assert len(extracted) == 2
    assert stopped.value.detail == {"chunks_done": 2, "chunks_total": 6}


@requires_db
@pytest.mark.asyncio
async def test_chunks_paid_for_before_a_stop_are_not_bought_again(tmp_path, monkeypatch, db):
    salt, digest = hash_password("pw")
    user = User(username="chunkowner", pw_salt=salt, pw_hash=digest)
    db.add(user)
    db.flush()
    project = Project(owner_id=user.id, name="chunks", doc={})
    db.add(project)
    db.commit()

    switch = Switch()
    sent, numbered = _numbered()

    def first_run(request):
        response = numbered(request)
        if len(sent) == 2:
            switch.on = True
        return response

    client, audio = _chunked(tmp_path, monkeypatch, first_run)
    client.chunk_cache = ChunkCache(project.id, client.variant)
    cancel.watch(switch.check)
    with pytest.raises(JobCancelled):
        await client.transcribe(audio)
    assert len(sent) == 2

    # The next run: not canceled, and it pays only for the four it lacks.
    switch.on = False
    again: list[str] = []

    def second_run(request):
        again.append("sent")
        return httpx.Response(200, json={"text": f"late{len(again)}."})

    client, audio = _chunked(tmp_path, monkeypatch, second_run)
    cache = ChunkCache(project.id, client.variant)
    client.chunk_cache = cache
    result = await client.transcribe(audio)

    assert len(again) == 4
    assert cache.hits == 2
    # In chunk order: the two kept answers first, then the four new ones.
    assert result.full_text == "1. 2. late1. late2. late3. late4."

    # Another recogniser's answer is not this one's.
    other, audio = _chunked(tmp_path, monkeypatch, second_run, model="duudlaga-stt-2")
    other.chunk_cache = ChunkCache(project.id, other.variant)
    await other.transcribe(audio)
    assert len(again) == 10


@pytest.mark.asyncio
async def test_a_canceled_job_makes_no_more_model_calls():
    sent: list[int] = []

    def handler(request):
        sent.append(1)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"ok": True})}, "finish_reason": "stop"}]},
        )

    client = OpenRouterClient(
        OpenRouterConfig(api_key="sk-test", model="test/model", base_url="https://fake/api/v1"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    switch = Switch()
    cancel.watch(switch.check)
    assert await client.complete_json("s", "u", SCHEMA, "n") == {"ok": True}

    switch.on = True
    with pytest.raises(JobCancelled):
        await client.complete_json("s", "u", SCHEMA, "n")
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_canceled_job_buys_no_more_voice_or_speech(tmp_path):
    sent: list[str] = []

    def handler(request):
        sent.append(request.url.path)
        return httpx.Response(200, content=b"never read")

    switch = Switch()
    switch.on = True
    cancel.watch(switch.check)

    tts = ElevenLabsTts(
        VoiceConfig(api_key="xi-test", voice_id="21m00Tcm4TlvDq8ikWAM", model="eleven_v3"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(JobCancelled):
        await tts.synthesize("Сайн байна уу.")

    audio = tmp_path / "audio.wav"
    _wav(audio, seconds=2.0)
    stt = ElevenLabsSttClient(
        ElevenLabsSttConfig(api_key="xi-test"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(JobCancelled):
        await stt.transcribe(audio)

    assert sent == []


@pytest.mark.asyncio
async def test_a_cancel_reaches_only_its_own_job():
    """Two jobs run side by side in one worker. Each watches its own flag, set
    in its own task, so canceling one leaves the other buying."""
    canceled, running = Switch(), Switch()
    canceled.on = True
    seen: dict[str, bool] = {}

    async def job(name: str, switch: Switch) -> None:
        cancel.watch(switch.check)
        await asyncio.sleep(0)
        try:
            cancel.check()
            seen[name] = False
        except JobCancelled:
            seen[name] = True

    await asyncio.gather(job("canceled", canceled), job("running", running))
    assert seen == {"canceled": True, "running": False}


# ---------------------------------------------------------------------------
# Stages that swallow their own failures must not swallow a cancel
# ---------------------------------------------------------------------------


class _Model:
    """An LLM client that answers by index and is canceled on call `stop_at`."""

    def __init__(self, stop_at: int) -> None:
        self.calls = 0
        self.stop_at = stop_at

    async def complete_json(self, system, user, schema, name, max_tokens=None):
        self.calls += 1
        if self.calls == self.stop_at:
            raise JobCancelled()
        lines = [int(x) for x in re.findall(r"^\[(\d+)\]", user, flags=re.MULTILINE)]
        return {"lines": [{"i": i, "text": f"орчуулга {i}"} for i in lines]}


@pytest.mark.asyncio
async def test_a_canceled_translation_returns_the_lines_it_already_bought(monkeypatch):
    """Each chunk is a paid call. A cancel after the first must not throw the
    first away — nor be read as that chunk failing and the run carried on."""
    monkeypatch.setattr(translate_mod, "MAX_ANSWER_TOKENS", 60)
    segments = [
        Segment(id=f"s{i}", start=float(i), end=float(i) + 2.0, text=f"Line number {i}.")
        for i in range(6)
    ]
    transcript = Transcript(language="en", segments=segments, full_text=" ".join(s.text for s in segments))
    chunks = translate_mod.plan_chunks(segments, list(range(6)))
    assert len(chunks) >= 3

    model = _Model(stop_at=2)
    translated, report = await translate_mod.translate_transcript(model, transcript)

    assert model.calls == 2  # the canceled call, and nothing after it
    assert report.stopped is True
    assert report.translated == len(chunks[0])
    kept = [s.id for s in translated.segments if s.translation]
    assert kept == [f"s{i}" for i in chunks[0]]
    assert report.to_dict()["stopped"] is True


@pytest.mark.asyncio
async def test_punctuation_does_not_swallow_a_cancel():
    """It is a quality step whose failures are logged and skipped — but a
    cancel is not a failed chunk, and skipping it bought every chunk after."""
    from app.ai.suggest import restore_sentences

    segments = [Segment(id=f"s{i}", start=float(i), end=float(i) + 1.0, text=f"word {i}") for i in range(3)]
    transcript = Transcript(language="mn", segments=segments, full_text="word 0 word 1 word 2")

    with pytest.raises(JobCancelled):
        await restore_sentences(_Model(stop_at=1), transcript)


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


@pytest.fixture
def owned_project(db):
    salt, digest = hash_password("pw")
    user = User(username="cancelowner", pw_salt=salt, pw_hash=digest)
    db.add(user)
    db.flush()
    row = Project(owner_id=user.id, name="cancel", doc={})
    db.add(row)
    db.commit()
    return row


def _running(db, project_id: str, kind: str, dedupe_key: str | None = None) -> Job:
    job_id = queue.enqueue(kind, project_id=project_id, dedupe_key=dedupe_key)
    job = queue.claim("worker-test")
    assert job is not None and job.id == job_id
    db.expire_all()
    return job


@requires_db
def test_a_canceled_job_says_how_far_it_got(db, owned_project, monkeypatch, tmp_path):
    from app import config, worker

    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    config.get_settings.cache_clear()

    async def stops(handle):
        raise JobCancelled({"chunks_done": 7, "chunks_total": 40})

    monkeypatch.setitem(worker.HANDLERS, "transcribe", stops)
    monkeypatch.setitem(worker.DISK_NEED_BYTES, "transcribe", 0)
    job = _running(db, owned_project.id, "transcribe")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    row = db.get(Job, job.id)
    assert row.state == "canceled"
    assert row.result["output"]["stopped"] == {"chunks_done": 7, "chunks_total": 40}
    config.get_settings.cache_clear()


@requires_db
def test_a_failed_attempt_is_requeued_with_a_backoff_by_the_worker(db, owned_project, monkeypatch, tmp_path):
    from app import config, worker

    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    config.get_settings.cache_clear()

    async def breaks(handle):
        raise RuntimeError("R2 blinked")

    monkeypatch.setitem(worker.HANDLERS, "export", breaks)
    monkeypatch.setitem(worker.DISK_NEED_BYTES, "export", 0)
    job = _running(db, owned_project.id, "export", dedupe_key=f"export:{owned_project.id}")

    before = time.time()
    asyncio.run(worker._run_job(job))

    db.expire_all()
    row = db.get(Job, job.id)
    assert row.state == "queued" and row.finished_at is None
    assert row.dedupe_key == f"export:{owned_project.id}"
    assert row.available_at >= before + queue.RETRY_BACKOFF_SEC
    assert [(h["attempt"], h["outcome"], h["error"]) for h in row.history] == [
        (1, "requeued", "RuntimeError: R2 blinked")
    ]
    config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# requeue: the same job, after a backoff, with its history
# ---------------------------------------------------------------------------


@requires_db
def test_a_failed_attempt_comes_back_as_the_same_job_after_a_backoff(db, owned_project):
    key = f"export:{owned_project.id}"
    job = _running(db, owned_project.id, "export", dedupe_key=key)
    assert job.started_at is not None

    before = time.time()
    assert queue.requeue(job.id, error="disk full", attempt=job.attempts) is True

    db.expire_all()
    row = db.get(Job, job.id)
    assert (row.state, row.dedupe_key, row.finished_at, row.claimed_by) == ("queued", key, None, None)
    assert before + queue.RETRY_BACKOFF_SEC <= row.available_at <= time.time() + queue.RETRY_BACKOFF_SEC
    (entry,) = row.history
    assert entry["attempt"] == 1 and entry["outcome"] == "requeued" and entry["error"] == "disk full"
    assert entry["started_at"] == pytest.approx(job.started_at) and entry["ended_at"] >= entry["started_at"]
    assert entry["worker"] == "worker-test"

    # A second click while it waits finds it, rather than starting a twin.
    assert queue.enqueue("export", project_id=owned_project.id, dedupe_key=key) == job.id
    # And no worker takes it before its time.
    assert queue.claim("worker-test") is None

    row.available_at = time.time() - 1
    db.commit()
    again = queue.claim("worker-test")
    assert again is not None and again.id == job.id and again.attempts == 2
    assert again.started_at > entry["started_at"]


def test_the_backoff_doubles_and_stops_growing():
    assert [queue.backoff_sec(n) for n in (1, 2, 3)] == [30.0, 60.0, 120.0]
    assert queue.backoff_sec(50) == queue.RETRY_BACKOFF_MAX_SEC


@requires_db
def test_every_attempt_is_kept_however_the_job_ends(db, owned_project):
    """`error` holds only the latest attempt's. A render that failed twice and
    then worked used to read as clean, its two failures recorded nowhere."""
    job = _running(db, owned_project.id, "export")
    for error in ("first failure", "second failure"):
        queue.requeue(job.id, error=error, attempt=job.attempts)
        db.expire_all()
        row = db.get(Job, job.id)
        row.available_at = None
        db.commit()
        job = queue.claim("worker-test")
    assert queue.finish(job.id, state="done", output={}, attempt=job.attempts) is True

    db.expire_all()
    row = db.get(Job, job.id)
    assert row.error is None
    assert [(h["attempt"], h["outcome"], h.get("error")) for h in row.history] == [
        (1, "requeued", "first failure"),
        (2, "requeued", "second failure"),
        (3, "done", None),
    ]


@requires_db
def test_a_job_stopped_by_a_deploy_is_taken_again_at_once(db, owned_project):
    """The attempt did nothing wrong; its worker was stopped. No backoff."""
    job = _running(db, owned_project.id, "export")
    assert queue.abandon(job.id, requeue=True, reason="deploy", attempt=job.attempts) is True

    db.expire_all()
    row = db.get(Job, job.id)
    assert row.state == "queued" and row.available_at is None
    assert [(h["outcome"], h["error"]) for h in row.history] == [("requeued", "deploy")]
    assert queue.claim("worker-test").id == job.id


@requires_db
def test_a_retry_waiting_out_its_backoff_is_not_a_stalled_queue(db, owned_project):
    """Counted as runnable, it reported a healthy idle worker as down for up
    to ten minutes after every failure."""
    job = _running(db, owned_project.id, "export")
    queue.requeue(job.id, error="x", attempt=job.attempts)
    overview = queue.queue_overview()
    assert overview["waiting"] == 1 and overview["stalled"] is False

    queue.enqueue("suggest", project_id=owned_project.id)
    assert queue.queue_overview()["stalled"] is True


@requires_db
def test_a_reaped_attempt_is_kept_in_the_history(db, owned_project):
    job = _running(db, owned_project.id, "export")
    row = db.get(Job, job.id)
    row.heartbeat_at = time.time() - 10_000
    db.commit()

    assert queue.reap_stale().requeued == 1
    db.expire_all()
    row = db.get(Job, job.id)
    assert row.available_at > time.time()
    assert [(h["attempt"], h["outcome"], h["error"]) for h in row.history] == [
        (1, "requeued", "Worker stopped responding")
    ]


@requires_db
def test_kept_chunks_go_with_their_project(db, owned_project):
    """The kept chunks go with the project: nothing else ever deletes them
    for a transcription that never finishes."""
    ChunkCache(owned_project.id, "duudlaga||mn").put("d" * 64, "text")
    assert db.query(SttChunk).count() == 1
    db.delete(db.get(Project, owned_project.id))
    db.commit()
    assert db.query(SttChunk).count() == 0
