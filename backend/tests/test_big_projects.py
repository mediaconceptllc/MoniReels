"""Big projects: what a read or a settings save costs (audit PERF-10, PERF-11).

Measured on a 2-hour project (4,000 lines, a 4.4 MB document) and an account
with 201 projects: GET /projects/{id} 762 ms, a PATCH of `crf` 1,048 ms with
the whole document sent back, the projects list 993 ms in 202 queries, and a
job poll 141 ms because its ownership check loaded the document.

The timings are a machine's; what these tests pin is the cause: which
queries run, and whether the document leaves Postgres at all.
"""
from __future__ import annotations

import ast
import json
import random
from pathlib import Path

import pytest
from sqlalchemy import event

from app.db import get_engine
from app.dbmodels import Output
from app.dbmodels import Project as Row
from app.jobs import queue
from app.models import SCHEMA_VERSION, Project, Segment, Transcript
from app.store import SETTINGS_FIELDS, load, save
from tests.conftest import requires_db
from tests.test_api import _auth, _project_with_video, _user, client  # noqa: F401 - fixture

pytestmark = requires_db

# In the transcript and nowhere else: a statement or a result that carries it
# carried the document.
MARK = "transcript-only-marker-7f3a"


class Sql:
    """Every statement run while recording, with the columns it returned."""

    def __init__(self):
        self.statements: list[tuple[str, object, list[str]]] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        names = [c[0] for c in cursor.description] if cursor.description else []
        self.statements.append((statement, parameters, names))

    def touching(self, table: str) -> list[str]:
        return [s for s, _, _ in self.statements if f"FROM {table}" in s or f"UPDATE {table}" in s]

    def returned(self, column: str) -> bool:
        """A result column named `column` — or `projects_doc`, as the ORM
        labels a column when it loads a whole row."""
        return any(
            name == column or name.endswith(f"_{column}")
            for _, _, names in self.statements
            for name in names
        )

    def carried(self, text: str) -> bool:
        return any(text in str(params) for _, params, _ in self.statements)


@pytest.fixture
def sql():
    recorder = Sql()
    engine = get_engine()
    event.listen(engine, "after_cursor_execute", recorder)
    yield recorder
    event.remove(engine, "after_cursor_execute", recorder)


def _with_transcript(db, owner, lines: int = 3):
    row = _project_with_video(db, owner)
    project = load(db, row.id)
    project.transcript = Transcript(
        language="mn",
        segments=[
            Segment(id=f"s{i}", start=i, end=i + 1, text=f"{MARK} {i}") for i in range(lines)
        ],
        full_text=MARK,
    )
    save(db, project)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# PERF-11: the projects list
# ---------------------------------------------------------------------------


def test_the_list_is_one_query_whatever_the_number_of_projects(client, db, sql):  # noqa: F811
    """1 + N: each row's outputs were counted with a query of their own, and
    every document was decoded to read four flags. 201 projects → 202 queries."""
    owner = _user(db, "lister")
    auth = _auth(client, "lister")
    rows = [_with_transcript(db, owner) for _ in range(6)]
    for i, row in enumerate(rows):
        for _ in range(i % 3):
            db.add(Output(project_id=row.id, kind="reel", title="t", r2_key=f"o/{row.id}/{_}"))
    db.commit()

    sql.statements.clear()
    response = client.get("/projects", headers=auth)
    assert response.status_code == 200

    # One statement reads the projects AND counts their outputs.
    statements = set(sql.touching("projects")) | set(sql.touching("outputs"))
    assert len(statements) == 1, statements
    assert not sql.returned("doc"), "the list fetched whole documents"
    counts = {item["id"]: item["n_outputs"] for item in response.json()}
    assert counts == {row.id: i % 3 for i, row in enumerate(rows)}


@pytest.mark.parametrize(
    "doc, expected",
    [
        ({}, (False, False, 0.0)),
        ({"video": None, "transcript": None, "suggestions": None}, (False, False, 0.0)),
        ({"transcript": {"segments": []}, "suggestions": {"shorts": []}}, (False, False, 0.0)),
        ({"transcript": {"segments": [{"id": "s"}]}, "suggestions": {"shorts": [{}]}}, (True, True, 0.0)),
        ({"video": {"duration_sec": 61.5}}, (False, False, 61.5)),
        ({"video": {"duration_sec": None}}, (False, False, 0.0)),
    ],
)
def test_the_list_reads_the_flags_the_document_has(client, db, doc, expected):  # noqa: F811
    """Read in SQL now, so every shape a stored document can be in is read
    the way `bool(list)` and `.get(..., 0.0)` read it in Python."""
    owner = _user(db, "flags")
    db.add(Row(owner_id=owner.id, name="raw", doc=doc))
    db.commit()
    (item,) = client.get("/projects", headers=_auth(client, "flags")).json()
    assert (item["has_transcript"], item["has_suggestions"], item["duration_sec"]) == expected


# ---------------------------------------------------------------------------
# PERF-11: routes that check ownership
# ---------------------------------------------------------------------------


def test_a_job_poll_reads_the_owner_not_the_document(client, db, sql):  # noqa: F811
    """Every open job card polls this; it loaded the project's document to
    compare one column. 141 ms against 5.6 ms on a 2-hour project."""
    owner = _user(db, "poller")
    other = _user(db, "stranger")
    row = _with_transcript(db, owner)
    job_id = queue.enqueue("export", project_id=row.id, db=db)
    db.commit()

    sql.statements.clear()
    assert client.get(f"/jobs/{job_id}", headers=_auth(client, "poller")).status_code == 200
    assert not sql.returned("doc")
    # Still nobody else's to see — and still a 404, not a 403.
    assert client.get(f"/jobs/{job_id}", headers=_auth(client, "stranger")).status_code == 404
    assert other is not None


def test_outputs_and_deletes_leave_the_document_in_postgres(client, db, sql, monkeypatch):  # noqa: F811
    """The project page asks for its outputs beside the project itself; both
    loaded the document, so a page view decoded it twice."""
    from app import r2

    monkeypatch.setattr(r2, "delete", lambda key: None)
    monkeypatch.setattr(r2, "delete_prefix", lambda prefix: 0)
    owner = _user(db, "outs")
    _user(db, "notmine")
    auth = _auth(client, "outs")
    row = _with_transcript(db, owner)
    out = Output(project_id=row.id, kind="reel", title="t", r2_key=f"o/{row.id}/a")
    db.add(out)
    db.commit()
    out_id = out.id

    sql.statements.clear()
    listed = client.get(f"/projects/{row.id}/outputs", headers=auth)
    assert [o["id"] for o in listed.json()] == [out_id]
    assert client.delete(f"/projects/{row.id}/outputs/{out_id}", headers=auth).status_code == 200
    assert client.delete(f"/projects/{row.id}", headers=auth).status_code == 200
    assert not sql.returned("doc")

    other = _with_transcript(db, owner)
    stranger = _auth(client, "notmine")
    assert client.get(f"/projects/{other.id}/outputs", headers=stranger).status_code == 404
    assert client.delete(f"/projects/{other.id}", headers=stranger).status_code == 404


def test_a_locked_read_always_reads_the_document():
    """A locked read is a read-modify-write of the document, and must read it
    under the lock — `doc=False` with `for_update` is refused, not ignored."""
    from app.store import get_row

    with pytest.raises(ValueError):
        get_row(None, "x", for_update=True, doc=False)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PERF-10: reading a project
# ---------------------------------------------------------------------------


def test_the_project_read_is_the_document_and_the_page_extras(client, db):  # noqa: F811
    """The document is serialized in one native pass and the extras spliced
    on: the result must be ONE valid JSON object holding exactly what
    `model_dump` holds, plus the extras, each key once."""
    owner = _user(db, "reader")
    row = _with_transcript(db, owner, lines=50)
    response = client.get(f"/projects/{row.id}", headers=_auth(client, "reader"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"

    pairs = json.loads(response.content, object_pairs_hook=lambda kv: kv)
    keys = [k for k, _ in pairs]
    assert len(keys) == len(set(keys)), "a key appears twice"
    body = response.json()
    document = load(db, row.id).model_dump(mode="json")
    extras = {"media", "jobs", "job_history_limit", "suggest_limits", "translation",
              "voice", "speakers", "spend"}
    assert set(body) == set(document) | extras
    assert {k: body[k] for k in document if k != "updated_at"} == {
        k: v for k, v in document.items() if k != "updated_at"
    }


def test_an_extra_key_may_not_repeat_a_document_field():
    from app.api.projects.documents import _json_object

    with pytest.raises(ValueError):
        _json_object('{"name":"a"}', {"name": "b"})
    assert json.loads(_json_object('{"name":"a"}', {"media": None}).body) == {"name": "a", "media": None}


# ---------------------------------------------------------------------------
# PERF-10: a settings save
# ---------------------------------------------------------------------------


def test_a_settings_save_answers_with_the_settings_not_the_document(client, db, sql):  # noqa: F811
    owner = _user(db, "saver")
    auth = _auth(client, "saver")
    row = _with_transcript(db, owner, lines=200)

    sql.statements.clear()
    response = client.patch(
        f"/projects/{row.id}", json={"export": {"crf": 27, "logo": {"opacity": 0.5}}}, headers=auth
    )
    assert response.status_code == 200
    answer = response.json()
    assert set(answer) == {"id", "name", "updated_at", *SETTINGS_FIELDS}
    assert answer["export"]["crf"] == 27 and answer["export"]["logo"]["opacity"] == 0.5
    assert MARK not in response.text

    # The transcript neither came out of Postgres nor went back in.
    assert not sql.returned("doc"), "the save read the whole document"
    assert not sql.carried(MARK), "the save wrote the transcript back"

    db.expire_all()
    saved = load(db, row.id)
    assert saved.export.crf == 27
    assert saved.export.logo.opacity == 0.5
    assert saved.export.logo.width_pct == 12.0  # a nested section still merges
    assert saved.transcript is not None and len(saved.transcript.segments) == 200
    assert saved.updated_at == answer["updated_at"]


def test_a_settings_save_renames_and_changes_language(client, db):  # noqa: F811
    owner = _user(db, "renamer")
    row = _with_transcript(db, owner)
    response = client.patch(
        f"/projects/{row.id}", json={"name": "шинэ нэр", "language": "en",
                                     "transition": {"duration": 0.3}},
        headers=_auth(client, "renamer"),
    )
    assert response.status_code == 200
    db.expire_all()
    saved = load(db, row.id)
    assert (saved.name, saved.language, saved.transition.duration) == ("шинэ нэр", "en", 0.3)
    assert db.get(Row, row.id).name == "шинэ нэр"  # the column, not only the document


def test_an_old_document_is_migrated_by_its_first_settings_save(client, db):  # noqa: F811
    """A document from an older schema must not be patched in place: its
    sections may not be in today's shape. It takes the whole-document path
    once, which migrates and saves it at the current version."""
    owner = _user(db, "oldtimer")
    row = Row(owner_id=owner.id, name="old", schema_version=3, doc={
        "video": {"path": "C:/x.mp4", "duration_sec": 5, "width": 1, "height": 1, "fps": 30,
                  "has_audio": True, "codec": "h264"},
        "transcript": {"language": "mn", "segments": [
            {"id": "s0", "start": 0, "end": 1, "text": MARK}], "full_text": MARK},
    })
    db.add(row)
    db.commit()

    response = client.patch(
        f"/projects/{row.id}", json={"export": {"crf": 25}}, headers=_auth(client, "oldtimer")
    )
    assert response.status_code == 200
    db.expire_all()
    stored = db.get(Row, row.id)
    assert stored.schema_version == SCHEMA_VERSION
    assert stored.doc["video"] is None  # the v3 → v4 step ran
    assert stored.doc["export"]["crf"] == 25
    assert stored.doc["transcript"]["segments"][0]["text"] == MARK


def test_someone_elses_project_is_still_not_found_on_save(client, db):  # noqa: F811
    owner = _user(db, "mine")
    _user(db, "yours")
    row = _with_transcript(db, owner)
    response = client.patch(
        f"/projects/{row.id}", json={"export": {"crf": 30}}, headers=_auth(client, "yours")
    )
    assert response.status_code == 404
    db.expire_all()
    assert load(db, row.id).export.crf == 20


def test_every_field_a_save_accepts_is_one_the_settings_path_writes():
    """The settings path writes `name` and SETTINGS_FIELDS and nothing else.
    A field added to the PATCH body and not here would be accepted, applied
    to the copy in memory, and silently never stored."""
    from app.schemas import UpdateProjectIn

    assert set(UpdateProjectIn.model_fields) == {"name", *SETTINGS_FIELDS}
    assert set(SETTINGS_FIELDS) <= set(Project.model_fields)


def test_every_settings_save_reads_under_the_lock():
    """The rule test_every_document_save_locks_the_row_it_read applies to
    `save`, for the settings path: whoever calls `save_settings` must have
    read with `load_settings`, which always takes the row lock."""
    offenders = []
    for path in Path("app").rglob("*.py"):
        if path.name == "store.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            names = {n.func.id for n in ast.walk(fn)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if "save_settings" in names and "load_settings" not in names:
                offenders.append(f"{path}:{fn.name}")
    assert offenders == []


# ---------------------------------------------------------------------------
# PERF-10: which lines an export uses
# ---------------------------------------------------------------------------


def _brute(segments, ranges):
    """The rule as it was written: every line against every range."""
    return [
        s.id for s in segments
        if (s.text or "").strip() and any(s.end > a and s.start < b for a, b in ranges)
    ]


def test_lines_used_answers_exactly_what_every_line_against_every_range_did():
    """Merged and bisected, so it must agree with the brute force on every
    shape: touching ranges, overlapping ones, nested, empty and reversed
    ranges, zero-length and backwards lines, lines with no words."""
    from types import SimpleNamespace

    from app.languages import lines_used

    rng = random.Random(1801)
    grid = [x / 2 for x in range(0, 41)]  # coarse, so edges coincide often
    for trial in range(3000):
        segments = []
        for i in range(rng.randint(0, 25)):
            a = rng.choice(grid)
            b = a + rng.choice([-0.5, 0.0, 0.5, 1.0, 2.5])
            text = rng.choice(["x", "", "  ", "y z"])
            segments.append(SimpleNamespace(id=f"s{i}", start=a, end=b, text=text))
        ranges = []
        for _ in range(rng.randint(0, 6)):
            a = rng.choice(grid)
            b = a + rng.choice([-1.0, 0.0, 0.5, 1.0, 3.0, 7.5]) if trial % 5 == 0 else a + rng.choice(
                [0.5, 1.0, 3.0, 7.5]
            )
            ranges.append((a, b))
        project = SimpleNamespace(transcript=SimpleNamespace(segments=segments))
        got = [s.id for s in lines_used(project, ranges)]
        assert got == _brute(segments, ranges), (segments, ranges)
