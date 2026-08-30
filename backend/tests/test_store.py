"""Project persistence against Postgres.

The desktop build's test wrote project.json to a temp dir. That store is
gone, so these cover what replaced it — and specifically the two properties
the hybrid column/JSONB layout exists to preserve: the domain document
survives a round trip intact, and the schema-version ladder still runs.
"""
from __future__ import annotations

import pytest

from app.dbmodels import User
from app.models import SCHEMA_VERSION, Cut, Project, Segment, ShortIdea, Suggestions, Transcript, VideoMeta
from app.security import hash_password
from app.store import ProjectNotFound, get_row, list_for_owner, load, save, summary
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def owner(db):
    salt, digest = hash_password("pw")
    user = User(username="owner", pw_salt=salt, pw_hash=digest, role="editor")
    db.add(user)
    db.commit()
    return user


def _video() -> VideoMeta:
    return VideoMeta(
        source_key="sources/p1/source.mp4",
        duration_sec=120.5,
        width=1920,
        height=1080,
        fps=30.0,
        has_audio=True,
        codec="h264",
        thumbnail_key="thumbnails/p1/thumb.jpg",
    )


def test_save_and_load_round_trip(db, owner):
    project = Project(name="Дугаар нэг")
    project.video = _video()
    project.transcript = Transcript(
        language="mn",
        segments=[Segment(id="s0", start=0.0, end=2.0, text="Сайн байна уу")],
        full_text="Сайн байна уу",
    )
    save(db, project, owner_id=owner.id)
    db.commit()

    loaded = load(db, project.id)
    assert loaded.name == "Дугаар нэг"
    assert loaded.video is not None and loaded.video.source_key == "sources/p1/source.mp4"
    # Cyrillic must survive the JSONB round trip byte for byte — every piece
    # of user-facing text in this product is Mongolian.
    assert loaded.transcript is not None
    assert loaded.transcript.segments[0].text == "Сайн байна уу"


def test_load_missing_raises(db):
    with pytest.raises(ProjectNotFound):
        load(db, "does-not-exist")


def test_load_wrong_owner_is_not_found(db, owner):
    """A project belonging to someone else must be indistinguishable from one
    that does not exist — a 403 would confirm it is real."""
    other_salt, other_hash = hash_password("pw")
    other = User(username="other", pw_salt=other_salt, pw_hash=other_hash)
    db.add(other)
    project = Project(name="Private")
    save(db, project, owner_id=owner.id)
    db.commit()

    assert load(db, project.id, owner.id).name == "Private"
    with pytest.raises(ProjectNotFound):
        load(db, project.id, other.id)


def test_schema_version_is_stamped(db, owner):
    project = Project(name="v")
    save(db, project, owner_id=owner.id)
    db.commit()
    assert get_row(db, project.id).schema_version == SCHEMA_VERSION


def test_legacy_document_is_migrated_on_read(db, owner):
    """A v3 document holds a local `path`; v4 holds an R2 key.

    A desktop path names a file no server has, so the video block is cleared
    and the user re-uploads — but the transcript is expensive to recreate and
    still correct, so it must survive.
    """
    project = Project(name="legacy")
    row = save(db, project, owner_id=owner.id)
    row.schema_version = 3
    row.doc = {
        "video": {
            "path": r"C:\Users\me\video.mp4",
            "duration_sec": 60.0, "width": 1920, "height": 1080, "fps": 30.0,
            "has_audio": True, "codec": "h264", "thumbnail_path": r"C:\Users\me\thumb.jpg",
        },
        "transcript": {
            "language": "mn",
            "segments": [{"id": "s0", "start": 0.0, "end": 1.0, "text": "хуучин"}],
            "full_text": "хуучин",
        },
    }
    db.commit()

    loaded = load(db, project.id)
    assert loaded.video is None
    assert loaded.transcript is not None
    assert loaded.transcript.segments[0].text == "хуучин"


def test_list_is_newest_first(db, owner):
    for name in ("first", "second", "third"):
        project = Project(name=name)
        save(db, project, owner_id=owner.id)
        db.commit()
    assert [r.name for r in list_for_owner(db, owner.id)] == ["third", "second", "first"]


def test_summary_does_not_require_a_full_document(db, owner):
    """The list view must not pay to deserialize every transcript and
    suggestion block it never shows."""
    project = Project(name="s")
    project.video = _video()
    project.suggestions = Suggestions(
        shorts=[
            ShortIdea(
                id="i1", title="t", hook_text="h", hook_quote="q",
                cuts=[Cut(start=0, end=5, role="hook", reason="r")],
                caption="c", why_it_works="w",
            )
        ]
    )
    row = save(db, project, owner_id=owner.id)
    row.video_key = "sources/x/source.mp4"
    db.commit()

    result = summary(row)
    assert result["has_video"] is True
    assert result["has_suggestions"] is True
    assert result["has_transcript"] is False
    assert result["duration_sec"] == 120.5
