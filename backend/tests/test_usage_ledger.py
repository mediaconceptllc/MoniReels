"""The spend ledger (app.metering → usage_events → GET /admin/usage).

What this pins, in the order it matters:

  * a job that FAILED or was CANCELED still records what it bought — the
    job result is written on the success path alone, which is the gap the
    ledger exists to close;
  * no purge touches the ledger;
  * an unknown price is NULL, never zero, from the gate to the report;
  * each provider is counted at its gate, and only for an answered request;
  * the report is admin-only and its totals are the sum of its rows.
"""
from __future__ import annotations

import asyncio
import json
import time
import wave
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app import metering
from app.ai.openrouter_client import OpenRouterClient, OpenRouterConfig
from app.api.routes_usage import window_start
from app.dbmodels import Job, Project, UsageEvent, User
from app.jobs import queue
from app.jobs.queue import JobCancelled
from app.security import hash_password
from app.stt.duudlaga_client import DuudlagaClient, DuudlagaConfig, DuudlagaError
from app.stt.elevenlabs_client import ElevenLabsError, ElevenLabsSttClient, ElevenLabsSttConfig
from app.tts.elevenlabs import ElevenLabsTts, TtsError, VoiceConfig
from app.video.capabilities import Capabilities
from tests.conftest import requires_db

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}
RATES = ("DUUDLAGA_USD_PER_MINUTE", "ELEVENLABS_STT_USD_PER_MINUTE", "ELEVENLABS_TTS_USD_PER_1K_CHARS")


@pytest.fixture(autouse=True)
def _no_rates(monkeypatch, tmp_path):
    """Unset unless a test says otherwise, whatever the machine's .env holds.
    And no meter left behind: a sync test's `start()` would otherwise stay
    set for every test after it."""
    from app.config import get_settings

    for name in RATES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    get_settings.cache_clear()
    token = metering._current.set(None)
    yield
    metering._current.reset(token)
    get_settings.cache_clear()


def _rates(monkeypatch, **values: str) -> None:
    from app.config import get_settings

    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def _llm(cost: float | None = 0.0012, prompt: int = 100, completion: int = 20) -> OpenRouterClient:
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cost is not None:
        usage["cost"] = cost

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "test/model",
                "choices": [{"message": {"content": json.dumps({"ok": True})}, "finish_reason": "stop"}],
                "usage": usage,
            },
        )

    return OpenRouterClient(
        OpenRouterConfig(api_key="sk-test", model="test/model", base_url="https://fake/api/v1"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _wav(path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))


def _flushed() -> list[dict]:
    """What a meter started in this test would write, without a database."""
    meter = metering.current()
    assert meter is not None
    return [
        {"provider": p, "kind": k, "model": m, "priced": priced, **vars(line)}
        for (p, k, m, priced), line in meter.lines.items()
    ]


# ---------------------------------------------------------------------------
# The worker: spend is written however the job ended
# ---------------------------------------------------------------------------


@pytest.fixture
def owner_project(db):
    salt, digest = hash_password("pw")
    user = User(username="ledgerowner", pw_salt=salt, pw_hash=digest)
    db.add(user)
    db.flush()
    row = Project(owner_id=user.id, name="ledger", doc={})
    db.add(row)
    db.commit()
    return user, row


def _running_job(db, project_id: str, kind: str) -> Job:
    job_id = queue.enqueue(kind, project_id=project_id)
    db.expire_all()
    job = db.get(Job, job_id)
    job.state = "running"
    db.commit()
    db.refresh(job)
    return job


def _ledger(db, job_id: str | None = None) -> list[UsageEvent]:
    db.expire_all()
    query = db.query(UsageEvent).order_by(UsageEvent.provider, UsageEvent.kind)
    if job_id is not None:
        query = query.filter(UsageEvent.job_id == job_id)
    return query.all()


def _spend_then(outcome: BaseException | None):
    """A handler that buys one model call and 90 seconds of speech, then
    ends however the test says."""

    async def handler(handle):
        await _llm().complete_json("s", "u", SCHEMA, "n")
        metering.record_stt(provider="duudlaga", model="", seconds=90.0)
        if outcome is not None:
            raise outcome
        return {"shorts": 0}

    return handler


@requires_db
def test_a_failed_job_still_records_what_it_spent(db, owner_project, monkeypatch):
    """The gap this ledger closes: spend lived only in the job's result, and
    that is written on the success path alone. A transcription that billed
    forty minutes and then failed recorded nothing anywhere."""
    from app import worker

    user, project = owner_project
    monkeypatch.setitem(worker.HANDLERS, "suggest", _spend_then(RuntimeError("late failure")))
    job = _running_job(db, project.id, "suggest")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    assert db.get(Job, job.id).state == "failed"
    stt, llm = _ledger(db, job.id)  # ordered by provider
    assert (llm.provider, llm.kind, llm.model) == ("openrouter", "llm", "test/model")
    assert (llm.requests, llm.prompt_tokens, llm.completion_tokens) == (1, 100, 20)
    assert llm.cost_usd == pytest.approx(0.0012)
    assert (stt.provider, stt.kind, stt.audio_seconds) == ("duudlaga", "stt", 90.0)
    for row in (llm, stt):
        assert (row.job_id, row.job_kind, row.project_id, row.user_id) == (
            job.id, "suggest", project.id, user.id,
        )


@requires_db
def test_a_canceled_job_still_records_what_it_spent(db, owner_project, monkeypatch):
    from app import worker

    _, project = owner_project
    monkeypatch.setitem(worker.HANDLERS, "suggest", _spend_then(JobCancelled()))
    job = _running_job(db, project.id, "suggest")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    assert db.get(Job, job.id).state == "canceled"
    assert [(r.provider, r.kind) for r in _ledger(db, job.id)] == [
        ("duudlaga", "stt"), ("openrouter", "llm"),
    ]


@requires_db
def test_a_finished_job_records_its_spend_and_keeps_its_summary(db, owner_project, monkeypatch):
    """The ledger is beside the job result, not instead of it — the job
    history still reads `output.llm`."""
    from app import worker

    _, project = owner_project
    monkeypatch.setitem(worker.HANDLERS, "suggest", _spend_then(None))
    job = _running_job(db, project.id, "suggest")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    done = db.get(Job, job.id)
    assert done.state == "done"
    assert done.result["output"]["llm"]["calls"] == 1
    assert len(_ledger(db, job.id)) == 2


@requires_db
def test_a_job_that_bought_nothing_writes_no_row(db, owner_project, monkeypatch):
    from app import worker

    _, project = owner_project

    async def free(handle):
        return {}

    monkeypatch.setitem(worker.HANDLERS, "suggest", free)
    job = _running_job(db, project.id, "suggest")

    asyncio.run(worker._run_job(job))

    assert _ledger(db) == []


@requires_db
def test_concurrent_jobs_keep_their_spend_apart(db, owner_project, monkeypatch):
    """Jobs run side by side in one worker process; a meter shared between
    them would bill one job's calls to the other."""
    from app import worker

    _, project = owner_project

    async def handler(handle):
        tokens = 1000 if handle.kind == "suggest" else 7
        await asyncio.sleep(0)
        await _llm(prompt=tokens).complete_json("s", "u", SCHEMA, "n")
        await asyncio.sleep(0)
        await _llm(prompt=tokens).complete_json("s", "u", SCHEMA, "n")
        return {}

    monkeypatch.setitem(worker.HANDLERS, "suggest", handler)
    monkeypatch.setitem(worker.HANDLERS, "translate", handler)
    a = _running_job(db, project.id, "suggest")
    b = _running_job(db, project.id, "translate")

    async def both():
        await asyncio.gather(worker._run_job(a), worker._run_job(b))

    asyncio.run(both())

    (row_a,) = _ledger(db, a.id)
    (row_b,) = _ledger(db, b.id)
    assert (row_a.requests, row_a.prompt_tokens) == (2, 2000)
    assert (row_b.requests, row_b.prompt_tokens) == (2, 14)


@requires_db
def test_a_ledger_that_cannot_be_written_does_not_break_the_job(db, owner_project, monkeypatch, caplog):
    """The job still settles, and the spend goes to the log instead."""
    import logging

    from app import worker

    caplog.set_level(logging.ERROR)

    _, project = owner_project
    monkeypatch.setitem(worker.HANDLERS, "suggest", _spend_then(None))

    def broken(*args, **kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(metering, "flush", broken)
    job = _running_job(db, project.id, "suggest")

    asyncio.run(worker._run_job(job))

    db.expire_all()
    assert db.get(Job, job.id).state == "done"
    assert "openrouter/llm test/model: 1 req" in caplog.text
    assert "duudlaga/stt -: 1 req, 90.0s, price unknown" in caplog.text


@requires_db
def test_a_project_deleted_mid_job_still_records_the_spend(db, owner_project):
    """No user to name, but the money was spent and is kept under its
    project id."""
    _, project = owner_project
    meter = metering.Meter()
    meter.add(provider="openrouter", kind="llm", model="m", cost=0.5, prompt=1)

    written = metering.flush(meter, job_id="j" * 32, job_kind="suggest", project_id="gone" * 8)

    assert written == 1
    (row,) = _ledger(db)
    assert (row.project_id, row.user_id, row.cost_usd) == ("gone" * 8, None, 0.5)


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------


@requires_db
def test_purge_never_deletes_the_ledger(db, owner_project):
    """Jobs are purged after job_keep_days and projects can be deleted; the
    record of money already spent survives both."""
    _, project = owner_project
    project_id = project.id
    job_id = _running_job(db, project_id, "suggest").id
    meter = metering.Meter()
    meter.add(provider="openrouter", kind="llm", model="m", cost=0.25, prompt=10)
    metering.flush(meter, job_id=job_id, job_kind="suggest", project_id=project_id)

    db.expire_all()
    row = db.get(Job, job_id)
    row.state = "done"
    row.finished_at = time.time() - 400 * 86400
    db.commit()

    assert queue.purge_old(30) == 1
    db.expire_all()
    assert db.get(Job, job_id) is None

    db.delete(db.get(Project, project_id))
    db.commit()

    (kept,) = _ledger(db)
    assert (kept.job_id, kept.project_id, kept.cost_usd) == (job_id, project_id, 0.25)


# ---------------------------------------------------------------------------
# Prices: never invented
# ---------------------------------------------------------------------------


def test_speech_without_a_configured_rate_is_unknown_not_free():
    metering.start()
    metering.record_stt(provider="duudlaga", model="", seconds=120.0)
    metering.record_tts(provider="elevenlabs", model="eleven_v3", characters=2000)

    lines = {line["kind"]: line for line in _flushed()}
    assert lines["stt"]["priced"] is False
    assert lines["tts"]["priced"] is False
    assert lines["stt"]["audio_seconds"] == 120.0
    assert lines["tts"]["characters"] == 2000


def test_a_configured_rate_prices_speech(monkeypatch):
    _rates(
        monkeypatch,
        DUUDLAGA_USD_PER_MINUTE="0.01",
        ELEVENLABS_STT_USD_PER_MINUTE="0.4",
        ELEVENLABS_TTS_USD_PER_1K_CHARS="0.3",
    )
    metering.start()
    metering.record_stt(provider="duudlaga", model="", seconds=120.0)
    metering.record_stt(provider="elevenlabs", model="scribe_v1", seconds=30.0)
    metering.record_tts(provider="elevenlabs", model="eleven_v3", characters=2500)

    cost = {(line["provider"], line["kind"]): line["cost_usd"] for line in _flushed()}
    assert cost[("duudlaga", "stt")] == pytest.approx(0.02)
    assert cost[("elevenlabs", "stt")] == pytest.approx(0.2)
    assert cost[("elevenlabs", "tts")] == pytest.approx(0.75)


def test_a_blank_rate_setting_reads_as_unset(monkeypatch):
    """A variable created on the platform and left empty must not stop the
    API from starting."""
    from app.config import get_settings

    _rates(monkeypatch, DUUDLAGA_USD_PER_MINUTE="")
    assert get_settings().duudlaga_usd_per_minute is None


@requires_db
def test_an_llm_answer_without_a_price_is_stored_as_unknown(db):
    """OpenRouter normally states a cost; when it does not, the ledger says
    NULL. The job summary keeps its old zero — that field means "no price
    added", the ledger's means "price unknown"."""
    async def go():
        metering.start()
        await _llm(cost=None).complete_json("s", "u", SCHEMA, "n")
        await _llm(cost=0.002).complete_json("s", "u", SCHEMA, "n")
        return metering.current()

    meter = asyncio.run(go())
    metering.flush(meter, job_id=None, job_kind="suggest", project_id=None)

    # Two lines, not one: a priced call and an unpriced one never share a
    # row, so a row's cost is the whole truth or NULL.
    rows = sorted(((r.requests, r.cost_usd) for r in _ledger(db)), key=lambda x: x[1] is None)
    assert rows == [(1, 0.002), (1, None)]


# ---------------------------------------------------------------------------
# The gates: counted where the provider answered, and only then
# ---------------------------------------------------------------------------


def _duudlaga(handler) -> DuudlagaClient:
    return DuudlagaClient(
        DuudlagaConfig(
            base_url="https://api.duudlaga.dev/v1", api_key="dk_test", max_audio_sec=60.0,
            upload_bitrate="", model="dl-1",
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_duudlaga_counts_the_seconds_it_answered(tmp_path):
    chunk = tmp_path / "chunk.wav"
    _wav(chunk, 2.5)
    metering.start()

    text = await _duudlaga(lambda r: httpx.Response(200, json={"text": "сайн"})).transcribe_chunk_text(chunk)

    assert text == "сайн"
    (line,) = _flushed()
    assert (line["provider"], line["kind"], line["model"]) == ("duudlaga", "stt", "dl-1")
    assert (line["requests"], line["audio_seconds"]) == (1, 2.5)


async def test_duudlaga_does_not_count_a_refused_request(tmp_path):
    """A 402 bought nothing, and a "no speech" verdict is an error status
    too — neither is billed audio."""
    chunk = tmp_path / "chunk.wav"
    _wav(chunk, 2.0)
    metering.start()

    with pytest.raises(DuudlagaError):
        await _duudlaga(
            lambda r: httpx.Response(402, json={"code": "insufficient_credits", "message": "x"})
        ).transcribe_chunk_text(chunk)
    silent = await _duudlaga(
        lambda r: httpx.Response(422, json={"code": "no_speech", "message": "x"})
    ).transcribe_chunk_text(chunk)

    assert silent == ""
    assert _flushed() == []


async def test_elevenlabs_stt_counts_only_an_answer(tmp_path):
    audio = tmp_path / "a.wav"
    _wav(audio, 4.0)
    metering.start()

    def client(handler):
        return ElevenLabsSttClient(
            ElevenLabsSttConfig(api_key="k", base_url="https://fake/v1"),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    with pytest.raises(ElevenLabsError):
        await client(lambda r: httpx.Response(500, json={})).transcribe(audio)
    assert _flushed() == []

    await client(lambda r: httpx.Response(200, json={"text": "сайн байна"})).transcribe(audio)
    (line,) = _flushed()
    assert (line["provider"], line["kind"], line["model"]) == ("elevenlabs", "stt", "scribe_v1")
    assert line["audio_seconds"] == 4.0


async def test_tts_counts_the_characters_it_spoke_once():
    """A 429 retried into a success is one line spoken, not two; an error
    spoke nothing."""
    metering.start()
    answers = iter([httpx.Response(429, json={}), httpx.Response(200, content=b"mp3")])

    async def no_sleep(seconds):
        return None

    def client(handler):
        return ElevenLabsTts(
            VoiceConfig(api_key="k", voice_id="Voice123", base_url="https://fake/v1"),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            sleep=no_sleep,
        )

    with pytest.raises(TtsError):
        await client(lambda r: httpx.Response(401, json={})).synthesize("Сайн байна уу")
    assert _flushed() == []

    await client(lambda r: next(answers)).synthesize("  Сайн байна уу  ")
    (line,) = _flushed()
    assert (line["provider"], line["kind"], line["model"]) == ("elevenlabs", "tts", "eleven_v3")
    assert (line["requests"], line["characters"]) == (1, len("Сайн байна уу"))


def test_nothing_is_counted_outside_a_job():
    """No meter, no row — and no error: the gates are shared with callers
    that are not jobs."""
    from contextvars import copy_context

    def outside():
        assert metering.current() is None
        metering.record_stt(provider="duudlaga", model="", seconds=1.0)
        metering.record_tts(provider="elevenlabs", model="m", characters=1)
        metering.record_llm(model="m", prompt=1, completion=1, cost=0.1)
        return metering.current()

    ctx = copy_context()
    ctx.run(metering._current.set, None)
    assert ctx.run(outside) is None


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db):
    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    app.state.capabilities = Capabilities(
        ffmpeg_available=True, ffmpeg_version="6.1", xfade_transitions=["fade"]
    )
    app.state.ffmpeg = None
    return TestClient(app)


def _user(db, username: str, role: str = "editor") -> User:
    salt, digest = hash_password("hunter2hunter2")
    user = User(username=username, pw_salt=salt, pw_hash=digest, role=role)
    db.add(user)
    db.commit()
    return user


def _auth(client: TestClient, username: str) -> dict:
    response = client.post("/auth/login", json={"username": username, "password": "hunter2hunter2"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _event(at: float, user_id: str | None, **fields) -> UsageEvent:
    base = {"provider": "openrouter", "kind": "llm", "model": "m", "requests": 1}
    return UsageEvent(created_at=at, user_id=user_id, job_kind="suggest", **{**base, **fields})


@requires_db
def test_the_report_sums_by_month_provider_and_user(client, db):
    admin = _user(db, "ledgeradmin", role="admin")
    bat = _user(db, "bat")
    now = datetime.now(UTC)
    this_month = window_start(now, 1).timestamp() + 3600
    last_month = window_start(now, 2).timestamp() + 3600
    too_old = window_start(now, 3).timestamp() + 3600
    db.add_all([
        # bat, this month, OpenRouter: two priced rows fold into one.
        _event(this_month, bat.id, prompt_tokens=100, completion_tokens=10, cost_usd=0.25),
        _event(this_month + 60, bat.id, requests=2, prompt_tokens=50, completion_tokens=5, cost_usd=0.5),
        # bat, this month, duudlaga: priced part and unpriced part.
        _event(this_month, bat.id, provider="duudlaga", kind="stt", model="",
               audio_seconds=60.0, cost_usd=0.01),
        _event(this_month, bat.id, provider="duudlaga", kind="stt", model="",
               requests=3, audio_seconds=30.25, cost_usd=None),
        # The admin, this month, TTS with no price at all.
        _event(this_month, admin.id, provider="elevenlabs", kind="tts", model="eleven_v3",
               characters=1200, cost_usd=None),
        # Nobody to name (project deleted mid-job), last month.
        _event(last_month, None, prompt_tokens=7, cost_usd=1.0),
        # Outside a two-month window.
        _event(too_old, bat.id, cost_usd=100.0),
    ])
    db.commit()

    r = client.get("/admin/usage?months=2", headers=_auth(client, "ledgeradmin"))
    assert r.status_code == 200, r.text
    body = r.json()

    key = {(row["month"], row["provider"], row["kind"], row["username"]): row for row in body["rows"]}
    this, last = now.strftime("%Y-%m"), datetime.fromtimestamp(last_month, UTC).strftime("%Y-%m")
    assert len(body["rows"]) == 4

    llm = key[(this, "openrouter", "llm", "bat")]
    assert (llm["requests"], llm["prompt_tokens"], llm["completion_tokens"]) == (3, 150, 15)
    assert llm["cost_usd"] == pytest.approx(0.75)
    assert llm["unpriced_requests"] == 0

    stt = key[(this, "duudlaga", "stt", "bat")]
    assert (stt["requests"], stt["audio_seconds"]) == (4, 90.2)
    # The known part, with the unknown counted beside it — not folded in as 0.
    assert stt["cost_usd"] == pytest.approx(0.01)
    assert stt["unpriced_requests"] == 3

    tts = key[(this, "elevenlabs", "tts", "ledgeradmin")]
    assert tts["cost_usd"] is None
    assert (tts["characters"], tts["unpriced_requests"]) == (1200, 1)

    orphan = key[(last, "openrouter", "llm", None)]
    assert (orphan["user_id"], orphan["cost_usd"]) == (None, 1.0)

    months = {m["month"]: m for m in body["month_totals"]}
    assert set(months) == {this, last}
    assert months[this]["cost_usd"] == pytest.approx(0.76)
    assert months[this]["unpriced_requests"] == 4
    assert months[this]["requests"] == 8

    totals = body["totals"]
    assert totals["cost_usd"] == pytest.approx(1.76)
    assert totals["requests"] == 9
    assert totals["unpriced_requests"] == 4
    assert totals["characters"] == 1200
    # The totals ARE the rows, summed — nothing the table does not show.
    for field in ("requests", "prompt_tokens", "completion_tokens", "characters", "unpriced_requests"):
        assert totals[field] == sum(row[field] for row in body["rows"])
    assert body["timezone"] == "UTC"
    assert body["rates"]["duudlaga_usd_per_minute"] is None
    # Newest month first.
    assert body["rows"][0]["month"] == this


@requires_db
def test_the_report_is_admin_only(client, db):
    """Spend by person is an admin's question; an editor is refused at the
    backend, whatever the interface shows."""
    _user(db, "ledgereditor")
    assert client.get("/admin/usage", headers=_auth(client, "ledgereditor")).status_code == 403
    assert client.get("/admin/usage").status_code == 401


@requires_db
def test_the_window_is_bounded(client, db):
    _user(db, "ledgeradmin2", role="admin")
    auth = _auth(client, "ledgeradmin2")
    assert client.get("/admin/usage?months=0", headers=auth).status_code == 422
    assert client.get("/admin/usage?months=37", headers=auth).status_code == 422
    empty = client.get("/admin/usage?months=1", headers=auth).json()
    assert empty["rows"] == [] and empty["month_totals"] == []
    assert empty["totals"]["cost_usd"] is None


def test_the_window_is_whole_utc_months():
    assert window_start(datetime(2026, 1, 20, 5, tzinfo=UTC), 1) == datetime(2026, 1, 1, tzinfo=UTC)
    assert window_start(datetime(2026, 1, 20, 5, tzinfo=UTC), 2) == datetime(2025, 12, 1, tzinfo=UTC)
    assert window_start(datetime(2026, 3, 31, 23, tzinfo=UTC), 15) == datetime(2025, 1, 1, tzinfo=UTC)
