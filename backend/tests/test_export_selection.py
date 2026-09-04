"""Exporting SOME of the model's ideas instead of all of them.

The suggestion cards were read-only: three shorts, three YouTube plans, and
one button that rendered every one of them. A producer who wanted the second
short had to render six videos and delete five — each one a full encode of a
42-minute source.

Two halves, and the split matters. The ROUTE refuses a selection that does not
match the project right now, because the producer is standing at the button.
The WORKER re-resolves the selection against the project as it finds it,
because a job can sit in the queue while somebody regenerates the suggestions
— and rendering whatever now occupies index 2 is the one outcome worth ruling
out.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.dbmodels import Job, Project, User
from app.export.pipeline import pick_ideas
from app.jobs import queue
from app.models import Cut, KeepRange, ShortIdea, Suggestions, VideoMeta, YoutubePlan
from app.security import hash_password
from app.store import load, save
from app.video.capabilities import Capabilities
from tests.conftest import requires_db

pytestmark = requires_db

os.environ.setdefault("R2_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("R2_ACCESS_KEY_ID", "testkey")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "testsecret")
os.environ.setdefault("R2_BUCKET", "testbucket")


def _short(idea_id: str, title: str) -> ShortIdea:
    return ShortIdea(
        id=idea_id, title=title, hook_text="h", hook_quote="q",
        cuts=[
            Cut(start=0.0, end=6.0, role="hook", reason="r"),
            Cut(start=20.0, end=40.0, role="payoff", reason="r"),
        ],
        caption="c", why_it_works="w",
    )


def _plan(title: str) -> YoutubePlan:
    return YoutubePlan(
        title=title, throughline="t",
        ranges=[KeepRange(start=0.0, end=300.0)], total_duration=300.0,
    )


def _suggestions() -> Suggestions:
    return Suggestions(
        shorts=[_short("s1", "Нэг"), _short("s2", "Хоёр"), _short("s3", "Гурав")],
        youtube=[_plan("Төлөвлөгөө А"), _plan("Төлөвлөгөө Б"), _plan("Төлөвлөгөө В")],
    )


# ==========================================================================
# pick_ideas — the worker's half
# ==========================================================================


def test_no_selection_is_every_idea():
    """An export queued before choosing existed carries no selection, and has
    to render exactly what it always did."""
    wanted, skipped = pick_ideas(_suggestions(), {})
    assert [s.id for s in wanted.shorts] == ["s1", "s2", "s3"]
    assert len(wanted.youtube) == 3
    assert skipped == []


def test_a_selection_narrows_to_what_was_named():
    payload = {"pick": {"shorts": ["s2"], "youtube": [{"i": 0, "title": "Төлөвлөгөө А"}]}}
    wanted, skipped = pick_ideas(_suggestions(), payload)
    assert [s.id for s in wanted.shorts] == ["s2"]
    assert [p.title for p in wanted.youtube] == ["Төлөвлөгөө А"]
    assert skipped == []


def test_naming_only_shorts_leaves_the_plans_alone():
    """Half a selection is still a selection: an omitted side means "all of
    that side", not "none of it"."""
    wanted, _ = pick_ideas(_suggestions(), {"pick": {"shorts": ["s1"]}})
    assert len(wanted.shorts) == 1
    assert len(wanted.youtube) == 3


def test_naming_only_plans_leaves_the_shorts_alone():
    """The mirror of the case above — the same rule read from the other side.
    Only one of the two branches was covered, so only one was really pinned."""
    wanted, _ = pick_ideas(_suggestions(), {"pick": {"youtube": [{"i": 0, "title": "Төлөвлөгөө А"}]}})
    assert [s.id for s in wanted.shorts] == ["s1", "s2", "s3"]
    assert len(wanted.youtube) == 1


def test_a_short_that_is_gone_is_skipped_and_named():
    """Not guessed at. The producer picked three and has to be able to see
    they got two."""
    payload = {"pick": {"shorts": ["s1", "s9"]}}
    wanted, skipped = pick_ideas(_suggestions(), payload)
    assert [s.id for s in wanted.shorts] == ["s1"]
    assert any("s9" in line for line in skipped)


def test_a_plan_whose_title_moved_is_skipped_rather_than_rendered():
    """THE case this pins. A YouTube plan has no id, so the selection names a
    position — and a regenerate between queueing and rendering puts a
    different plan at that position. Rendering it would hand the producer a
    video they never asked for, with nothing anywhere saying so."""
    payload = {"pick": {"youtube": [{"i": 1, "title": "Төлөвлөгөө Б"}]}}
    moved = Suggestions(
        shorts=_suggestions().shorts,
        youtube=[_plan("Огт өөр"), _plan("Бас өөр"), _plan("Гурав дахь")],
    )
    wanted, skipped = pick_ideas(moved, payload)
    assert wanted.youtube == []
    assert any("Төлөвлөгөө Б" in line for line in skipped)


def test_an_index_past_the_end_is_skipped():
    wanted, skipped = pick_ideas(_suggestions(), {"pick": {"youtube": [{"i": 7, "title": "x"}]}})
    assert wanted.youtube == []
    assert skipped


def test_a_selection_that_matches_nothing_returns_nothing_to_render():
    """The worker raises on this rather than rendering an empty export — the
    handler above turns it into a failed job naming the ideas."""
    wanted, skipped = pick_ideas(_suggestions(), {"pick": {"shorts": ["gone"], "youtube": []}})
    assert not wanted.shorts and not wanted.youtube
    assert skipped


# ==========================================================================
# The route — the producer's half
# ==========================================================================


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


@pytest.fixture
def project(client, db):
    salt, digest = hash_password("hunter2hunter2")
    user = User(username="exportowner", pw_salt=salt, pw_hash=digest)
    db.add(user)
    db.flush()
    row = Project(owner_id=user.id, name="export selection", doc={})
    db.add(row)
    db.commit()

    doc = load(db, row.id)
    doc.video = VideoMeta(
        source_key=f"sources/{row.id}/source.mp4", duration_sec=2540.0,
        width=1920, height=1080, fps=30.0, has_audio=True, codec="h264",
    )
    doc.suggestions = _suggestions()
    save(db, doc)
    db.commit()

    login = client.post(
        "/auth/login", json={"username": "exportowner", "password": "hunter2hunter2"}
    )
    assert login.status_code == 200, login.text
    return row, {"Authorization": f"Bearer {login.json()['token']}"}


def _job(db, job_id: str) -> Job:
    db.expire_all()
    return db.get(Job, job_id)


def test_no_body_still_exports_everything(client, project, db):
    """The button that predates choosing must keep working untouched."""
    row, auth = project
    r = client.post(f"/projects/{row.id}/export-all", headers=auth)
    assert r.status_code == 200, r.text
    assert queue.payload_of(_job(db, r.json()["job_id"])) == {}


def test_a_selection_reaches_the_job(client, project, db):
    row, auth = project
    r = client.post(
        f"/projects/{row.id}/export-all", headers=auth, json={"shorts": ["s2", "s3"]}
    )
    assert r.status_code == 200, r.text
    assert queue.payload_of(_job(db, r.json()["job_id"]))["pick"]["shorts"] == ["s2", "s3"]


def test_a_chosen_plan_carries_its_title(client, project, db):
    """The index alone is not identity — see the worker test above."""
    row, auth = project
    r = client.post(f"/projects/{row.id}/export-all", headers=auth, json={"youtube": [1]})
    assert r.status_code == 200, r.text
    picked = queue.payload_of(_job(db, r.json()["job_id"]))["pick"]["youtube"]
    assert picked == [{"i": 1, "title": "Төлөвлөгөө Б"}]


def test_an_unknown_short_is_refused_at_the_button(client, project):
    """Refused here, not dropped twenty minutes into a render."""
    row, auth = project
    r = client.post(f"/projects/{row.id}/export-all", headers=auth, json={"shorts": ["nope"]})
    assert r.status_code == 422
    assert "nope" in r.json()["detail"]


def test_an_out_of_range_plan_is_refused(client, project):
    row, auth = project
    r = client.post(f"/projects/{row.id}/export-all", headers=auth, json={"youtube": [9]})
    assert r.status_code == 422


def test_an_empty_selection_is_refused(client, project):
    """Different from sending no body at all: this one asked for nothing."""
    row, auth = project
    r = client.post(
        f"/projects/{row.id}/export-all", headers=auth, json={"shorts": [], "youtube": []}
    )
    assert r.status_code == 400


def test_two_different_selections_are_two_jobs(client, project):
    """The selection is part of the job's identity.

    Sharing one dedupe key would hand the second request the FIRST job's id —
    the producer would watch a progress bar for a render of something else.
    """
    row, auth = project
    first = client.post(f"/projects/{row.id}/export-all", headers=auth, json={"shorts": ["s1"]})
    second = client.post(f"/projects/{row.id}/export-all", headers=auth, json={"shorts": ["s2"]})
    assert first.json()["job_id"] != second.json()["job_id"]


def test_the_same_selection_twice_is_one_job(client, project):
    """A double click is still one export."""
    row, auth = project
    first = client.post(f"/projects/{row.id}/export-all", headers=auth, json={"shorts": ["s1"]})
    second = client.post(f"/projects/{row.id}/export-all", headers=auth, json={"shorts": ["s1"]})
    assert first.json()["job_id"] == second.json()["job_id"]


def test_a_selection_and_the_whole_lot_are_two_jobs(client, project):
    row, auth = project
    everything = client.post(f"/projects/{row.id}/export-all", headers=auth)
    one = client.post(f"/projects/{row.id}/export-all", headers=auth, json={"shorts": ["s1"]})
    assert everything.json()["job_id"] != one.json()["job_id"]


def test_another_owners_project_is_not_found(client, project, db):
    """Ownership is checked before the selection is even read."""
    row, _ = project
    salt, digest = hash_password("hunter2hunter2")
    db.add(User(username="stranger", pw_salt=salt, pw_hash=digest))
    db.commit()
    login = client.post(
        "/auth/login", json={"username": "stranger", "password": "hunter2hunter2"}
    )
    auth = {"Authorization": f"Bearer {login.json()['token']}"}
    r = client.post(f"/projects/{row.id}/export-all", headers=auth, json={"shorts": ["s1"]})
    assert r.status_code == 404
