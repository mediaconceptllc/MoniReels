"""Captures the API's real JSON so the frontend's types can be checked against it.

CI runs `typecheck`, `lint` and `build` on the frontend. All three ask whether
the frontend agrees with ITSELF; nothing asks whether the JSON the backend
actually returns matches the types the components read. Remove a field or
rename one and CI stays green — the break appears in a user's browser, as
`undefined` where a number should be.

This test drives the real app through TestClient, captures every response
`frontend/src/lib/api.ts` describes, and writes them to `frontend/.shape/`.
`npm run verify-shape` then assigns each one to its declared type, so a
mismatch is a compile error in CI instead of a blank field in production.

Two things make the files safe to commit:

  * the data is SYNTHETIC — built here, not a real project;
  * every scalar is replaced by a placeholder of the same type before the
    file is written, and `_assert_no_values` refuses to write a file that
    still holds one.

That second rule is what keeps the fixtures stable. A fixture carrying real
values changes whenever the data does, and CI compares these files to the
committed ones byte for byte — so every unrelated run would turn red while
the CONTRACT had not moved at all.

Regenerating: run this test and commit `frontend/.shape/`. It is a shared
artefact of the two halves, which is exactly why it lives in the repo.
"""
from __future__ import annotations

import json as _json
import os
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.dbmodels import Job, Output, Project, User
from app.jobs import kinds
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
from app.video.capabilities import Capabilities
from tests.conftest import requires_db

pytestmark = requires_db

# Presigning is pure local signing — boto3 never reaches the network for it —
# so a fake account exercises the real code path and produces a real URL.
os.environ.setdefault("R2_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("R2_ACCESS_KEY_ID", "testkey")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "testsecret")
os.environ.setdefault("R2_BUCKET", "testbucket")

SHAPE_DIR = Path(__file__).resolve().parents[2] / "frontend" / ".shape"

#: Fields whose VALUE is part of the contract rather than an example of one.
#: A union type (`"reel" | "youtube" | "export"`) is only really checked if a
#: real member survives into the fixture; blanked to `""` it checks nothing.
#:
#: Matched by PATH, exactly as `_MAPS` is, and for the same reason: `name` is
#: a three-way union on a Capability and free text on a project. Only the
#: names that are a union EVERYWHERE stand alone.
_LITERAL = frozenset({
    "role", "state", "kind", "source", "orientation", "portrait_fill",
    "position", "capabilities.name",
})

#: `Record<string, …>` in api.ts: the KEYS are data, not field names, so they
#: are collapsed to a single `*`. Registered by PATH, never by name — a map
#: called `counts` and a contract object called `counts` are different things,
#: and matching on the bare name would flatten the second one away.
#:
#: Forgetting one is caught below by `_assert_no_values`, which refuses any
#: key that is not `*` or a plain identifier.
_MAPS = {
    "counts": 1,   # QueueStatus.counts — one entry per job state present
    "result": 1,   # Job.result — whatever the handler returned
}

_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


_INDEX_RE = re.compile(r"\[\d+\]")


def _matches(path: str, names) -> bool:
    """Path match, with list indices removed first.

    The two walkers below number list elements differently — one keeps the
    parent's path, the other appends `[i]` so an error can point at the
    element — and a registration like `capabilities.name` has to mean the
    same thing to both, or the guard rejects exactly what the normaliser was
    told to keep.
    """
    bare = _INDEX_RE.sub("", path)
    return any(bare == n or bare.endswith("." + n) for n in names)


def _map_depth(path: str) -> int:
    for name, depth in _MAPS.items():
        if path == name or path.endswith("." + name):
            return depth
    return 0


def _collapse(d: dict, depth: int) -> dict:
    if not d:
        return {}
    v = next(iter(d.values()))
    if depth > 1 and isinstance(v, dict):
        v = _collapse(v, depth - 1)
    return {"*": v}


def _stable(v, path=""):
    """Strips values, keeps shape."""
    if isinstance(v, dict):
        d = {k: _stable(v[k], f"{path}.{k}" if path else k) for k in v}
        depth = _map_depth(path)
        return _collapse(d, depth) if depth else d
    if isinstance(v, list):
        # De-duplicate AFTER normalising: elements of the same shape become
        # one, genuinely different ones (an optional field present in some
        # and null in others) all survive. Then sort, because element ORDER
        # is not part of the contract — verify-shape reads these with [0],
        # .map and .length — while it can drift from an incomplete SQL
        # ordering, which would redden CI without the contract moving.
        uniq: list = []
        for x in v:
            n = _stable(x, path)
            if n not in uniq:
                uniq.append(n)
        uniq.sort(key=lambda x: _json.dumps(x, sort_keys=True, ensure_ascii=False))
        return uniq
    if v is None or _matches(path, _LITERAL):
        # `null` stays: whether a field MAY be null is part of the contract,
        # and TypeScript checks exactly that.
        return v
    if isinstance(v, bool):  # bool is a subclass of int — test it first
        return False
    if isinstance(v, int):
        return 0
    if isinstance(v, float):
        return 0.0
    if isinstance(v, str):
        return ""
    return v


def _assert_no_values(v, name, path=""):
    """Proves the file about to be written holds no data.

    The normaliser can break quietly — a new container type, a branch nobody
    updated — and the files would start carrying values again, turning CI red
    weeks later for no reason anyone could see. This checks the PRODUCT
    instead of trusting the process.

    Data hides in KEYS as well as values: `{"9f3a…": 2}` passes any check on
    values while depending entirely on which rows exist. So every key must be
    either `*` (a collapsed map) or a plain identifier — which is what a field
    name looks like and what a database id does not.
    """
    if isinstance(v, dict):
        for k, x in v.items():
            assert k == "*" or _FIELD_RE.fullmatch(k), (
                f"{name}{path}.{k}: this key is DATA — register the map in "
                f"`_MAPS` (anything typed `Record<string, …>` in api.ts must "
                f"collapse to `*`)"
            )
            _assert_no_values(x, name, f"{path}.{k}")
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _assert_no_values(x, name, f"{path}[{i}]")
    elif not _matches(path, _LITERAL):
        blank = (
            v is None
            or v is False
            or (type(v) is str and v == "")
            or (type(v) is int and v == 0)
            or (type(v) is float and v == 0.0)
        )
        assert blank, f"{name}{path}: a value survived into the fixture ({v!r})"


def _dump(name: str, data) -> None:
    norm = _stable(data)
    _assert_no_values(norm, name)
    text = _json.dumps(norm, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    # Idempotent: normalising the result again must not change it. If it does,
    # the normaliser is itself unstable — which is the exact fault this file
    # exists to keep out of CI.
    assert _stable(_json.loads(text)) == norm, f"{name}: normalisation is unstable"
    SHAPE_DIR.mkdir(parents=True, exist_ok=True)
    (SHAPE_DIR / name).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Synthetic project
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


def _admin(db) -> User:
    salt, digest = hash_password("hunter2hunter2")
    user = User(username="shapeadmin", pw_salt=salt, pw_hash=digest, role="admin")
    db.add(user)
    db.commit()
    return user


def _filled_project(db, owner_id: str) -> Project:
    """A project at the far end of the pipeline.

    Every optional field is filled in on purpose. A fixture taken from an
    empty project types-checks nothing: `video: null` satisfies
    `VideoMeta | null` while saying nothing about VideoMeta's own fields, and
    the nested reads in verify-shape are skipped rather than failed.
    """
    row = Project(
        owner_id=owner_id,
        name="shape fixture",
        video_key="sources/shape/source.mp4",
        thumbnail_key="thumbnails/shape/thumb.jpg",
        audio_key="audio/shape/audio.wav",
        doc={},
    )
    db.add(row)
    db.commit()

    project = load(db, row.id)
    project.video = VideoMeta(
        source_key="sources/shape/source.mp4", duration_sec=1800.0,
        width=1920, height=1080, fps=30.0, has_audio=True, codec="h264",
        thumbnail_key="thumbnails/shape/thumb.jpg", audio_key="audio/shape/audio.wav",
    )
    project.transcript = Transcript(
        language="mn",
        segments=[
            Segment(id="s1", start=0.0, end=4.0, text="Сайн байна уу.", speaker="0"),
            # A second segment with `speaker: null`, so the fixture carries
            # BOTH members of `string | null` and the type is really tested.
            Segment(id="s2", start=4.0, end=9.0, text="Тавтай морил.", speaker=None),
        ],
        full_text="Сайн байна уу. Тавтай морил.",
        timings_estimated=True,
    )
    project.suggestions = Suggestions(
        shorts=[
            ShortIdea(
                id=f"idea{i}", title=f"Санал {i}", hook_text="Дэгээ", hook_quote="Ишлэл",
                cuts=[
                    Cut(start=0.0, end=6.0, role="hook", reason="эхлэл"),
                    Cut(start=20.0, end=32.0, role="context", reason="дэвсгэр"),
                    Cut(start=60.0, end=75.0, role="proof", reason="нотолгоо"),
                    Cut(start=90.0, end=110.0, role="payoff", reason="оргил"),
                ],
                on_screen_texts=["Бичвэр"], b_roll=["Гар зураг"],
                caption="Тайлбар", hashtags=["#монгол"], why_it_works="Учир нь",
            )
            for i in range(3)
        ],
        youtube=[
            YoutubePlan(
                title=f"Төлөвлөгөө {i}", throughline="Гол утга",
                ranges=[KeepRange(start=0.0, end=300.0, reason="эхлэл")],
                total_duration=300.0,
            )
            for i in range(3)
        ],
    )
    project.clips = [
        Clip(id="c1", source_path="sources/shape/source.mp4", start=0.0, end=12.0, order=0),
        Clip(id="c2", source_path="sources/shape/source.mp4", start=40.0, end=55.0, order=1),
    ]
    project.export.use_intro = True
    save(db, project)

    db.add_all([
        Output(
            project_id=row.id, kind="reel", title="Богино 1",
            r2_key=f"outputs/{row.id}/reel_1.mp4", srt_key=f"outputs/{row.id}/reel_1.srt",
            size_bytes=4_000_000, duration_sec=42.0,
        ),
        # No .srt on this one, so `srt_url: string | null` carries both members.
        Output(
            project_id=row.id, kind="youtube", title="Хураангуй 1",
            r2_key=f"outputs/{row.id}/youtube_1.mp4", srt_key=None,
            size_bytes=90_000_000, duration_sec=610.0,
        ),
    ])
    db.commit()
    return row


# ---------------------------------------------------------------------------


def test_frontend_contract_shapes_are_captured(client, db, monkeypatch):
    """Every response `api.ts` declares a type for, captured in one place."""
    from app import r2

    # Two routes refuse to proceed until the browser's direct PUT has landed.
    # Whether an object exists is a storage fact, not part of the JSON
    # contract, so it is the one thing faked here — everything else is the
    # real handler answering a real request.
    monkeypatch.setattr(r2, "exists", lambda key: True)

    user = _admin(db)
    login = client.post(
        "/auth/login", json={"username": "shapeadmin", "password": "hunter2hunter2"}
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    row = _filled_project(db, user.id)

    def get(path: str) -> dict | list:
        r = client.get(path, headers=auth)
        assert r.status_code == 200, f"GET {path} -> {r.status_code} {r.text}"
        return r.json()

    def send(method: str, path: str, **kw) -> dict | list:
        r = client.request(method, path, headers=auth, **kw)
        assert r.status_code in (200, 201), f"{method} {path} -> {r.status_code} {r.text}"
        return r.json()

    # A job in every state the frontend renders, so `Job` is captured with a
    # result and an error present rather than both null.
    db.add_all([
        Job(
            id="shapejob1", project_id=row.id, kind="transcribe", state="done",
            progress=1.0, stage="done", message="Дууссан",
            result={"payload": {}, "output": {"segments": 2}},
            attempts=1, finished_at=time.time(),
        ),
        Job(
            id="shapejob2", project_id=row.id, kind="export", state="failed",
            progress=0.4, stage="rendering", message="Дүрслэл",
            error="RuntimeError: жишээ", attempts=2,
        ),
    ])
    db.commit()

    shape: dict = {
        # auth
        "token": login.json(),
        "me": get("/auth/me"),
        # projects
        "projects": get("/projects"),
        "project": get(f"/projects/{row.id}"),
        "create_project": send(
            "POST", "/projects",
            json={"name": "shape upload", "filename": "clip.mp4", "size_bytes": 1024},
        ),
        "project_patched": send(
            "PATCH", f"/projects/{row.id}", json={"name": "shape fixture"}
        ),
        "transcript_updated": send(
            "PUT", f"/projects/{row.id}/transcript",
            json={"segments": [{"id": "s1", "text": "Сайн байна уу."}]},
        ),
        "ranges_selected": send(
            "POST", f"/projects/{row.id}/select", json={"ranges": [[0.0, 12.0]]}
        ),
        # outputs
        "outputs": get(f"/projects/{row.id}/outputs"),
        # jobs
        "job": get("/jobs/shapejob1"),
        "job_failed": get("/jobs/shapejob2"),
        "queue": get("/jobs/queue"),
        "job_canceled": send("POST", "/jobs/shapejob2/cancel"),
        # admin + settings
        "provider_settings": get("/admin/settings"),
        # Synthetic keys, never used: the pipeline routes below only enqueue
        # a job, and they refuse to do even that while the capability they
        # need reports itself unconfigured.
        "provider_settings_saved": send(
            "PUT", "/admin/settings",
            json={
                "openrouter_model": "test/model",
                "openrouter_api_key": "sk-shape-fixture-not-a-real-key",
                "duudlaga_api_key": "shape-fixture-not-a-real-key",
                "stt_provider": "duudlaga",
            },
        ),
        "providers": get("/admin/providers"),
        "readiness": get("/projects/providers/status"),
        # Nothing uploaded yet, so every slot is null — which is the half of
        # `BrandLogo | null` a fresh deployment shows. The filled half is
        # captured below, once an asset exists.
        "brand_empty": get("/admin/brand"),
        "brand_upload_url": send(
            "POST", "/admin/brand/logo/upload-url", json={"content_type": "image/png"}
        ),
        # subtitles
        "fonts": get("/projects/subtitle/fonts"),
    }

    # The font comes from the SERVER's installed list, not from the project's
    # default: the default names a family the Dockerfile installs, which the
    # machine running this test need not have.
    # a family is only accepted if the render image installs it, and this
    # machine's fontconfig is not the one that decides the contract.
    style = {**shape["project"]["subtitle_style"], "font_family": shape["fonts"]["families"][0]}
    template = send(
        "POST", "/projects/subtitle/templates", json={"name": "Шаблон", "style": style}
    )
    shape["template_saved"] = template
    # Captured with a template IN it. An empty list satisfies
    # `SubtitleTemplate[]` while checking nothing about SubtitleTemplate.
    shape["templates"] = get("/projects/subtitle/templates")
    shape["template_deleted"] = client.request(
        "DELETE", f"/projects/subtitle/templates/{template['id']}", headers=auth
    ).json()
    # The key must be the one the presign handed out: the route refuses a key
    # that is not this asset's, so an invented one captures a 400 rather than
    # the contract.
    shape["brand_saved"] = send(
        "PUT", "/admin/brand/logo", json={"key": shape["brand_upload_url"]["key"]}
    )
    # And again with the slot filled, so `BrandLogo`'s own fields are checked
    # rather than only the `| null` beside them.
    shape["brand"] = get("/admin/brand")
    shape["output_deleted"] = client.request(
        "DELETE", f"/projects/{row.id}/outputs/{shape['outputs'][0]['id']}", headers=auth
    ).json()

    # The pipeline starts: three routes with one shape between them, captured
    # once each because a rename in any of them silently strips the panel of
    # the id it needs to follow the job.
    # All four say `{ job_id }`, and each is declared separately in api.ts —
    # a rename in any one of them leaves that button unable to follow its job.
    shape["transcribe_started"] = send("POST", f"/projects/{row.id}/transcribe")
    shape["suggest_started"] = send("POST", f"/projects/{row.id}/suggest")
    shape["export_started"] = send("POST", f"/projects/{row.id}/export")
    shape["export_all_started"] = send("POST", f"/projects/{row.id}/export-all")

    created = shape["create_project"]["project_id"]
    shape["upload_complete"] = send("POST", f"/projects/{created}/upload-complete")
    shape["project_deleted"] = client.request(
        "DELETE", f"/projects/{created}", headers=auth
    ).json()

    _dump("contracts.json", shape)

    # Enumerations the frontend mirrors as literal unions and label maps. A
    # kind added to the backend and not to JOB_LABELS shows an English
    # identifier to a user of a product whose every other string is Mongolian
    # — which is precisely how `audio` shipped without a label.
    _dump("registry.json", {
        "job_kinds": {k: 0 for k in kinds.KINDS},
        "output_kinds": {o["kind"]: 0 for o in shape["outputs"]},
        "cut_roles": {c["role"]: 0 for c in shape["project"]["suggestions"]["shorts"][0]["cuts"]},
    })


def test_the_captured_files_are_committed():
    """The fixtures are only a CI check if they are IN the repository.

    Generated into .gitignore they would exist on the machine that ran the
    test and nowhere else, which is how the sibling project's version of this
    went a year without running in CI.
    """
    for name in ("contracts.json", "registry.json"):
        assert (SHAPE_DIR / name).is_file(), (
            f"{name} is missing — run this module and commit frontend/.shape/"
        )
