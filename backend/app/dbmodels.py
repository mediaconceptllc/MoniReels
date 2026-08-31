"""ORM schema. Distinct from app.models, which stays the *domain* shape
(Transcript / Suggestions / Clip) shared with the AI and export pipelines.

Projects are stored hybrid on purpose: the handful of fields anything ever
filters or sorts by get real columns, and the rest of the domain document
lives in one JSONB `doc`. That keeps app.models.migrate_project_dict — a
working, tested schema-version ladder — usable unchanged, instead of
re-deriving every nested field as a table for queries nobody makes.
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uid() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    # Constrained to [A-Za-z0-9._-] at the schema layer (app.schemas.Username):
    # a username reaches R2 object keys, and a slash or a space there either
    # forges a path or breaks the signature.
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    pw_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    pw_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # When the password last changed. Kept for the audit trail only — the
    # actual token check uses token_serial, because a timestamp comparison
    # cannot express "issued in the same second as the change".
    pw_changed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Bumped on every password change; a token carries the serial it was
    # issued under and is refused once it no longer matches. This is what
    # makes "my account was compromised, I changed my password" actually end
    # the attacker's session — a timestamp cannot, because a JWT's `iat` has
    # one-second resolution and a token issued in that same second would
    # survive. (test_changing_a_password_invalidates_older_tokens found this
    # as a real flake before the serial existed.)
    token_serial: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="editor", server_default="editor")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)

    projects: Mapped[list[Project]] = relationship(back_populates="owner")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    owner_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # R2 object key of the uploaded source. The browser PUTs straight to a
    # presigned URL; this column is the only record the API keeps of where
    # the bytes landed. Keys are derived from the project id and never
    # renamed — renaming a project must not orphan its objects.
    video_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    thumbnail_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audio_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=4, server_default="4")
    # The app.models.Project document minus id/name/timestamps: video,
    # transcript, suggestions, clips, transition, subtitle_style, export.
    doc: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)

    owner: Mapped[User] = relationship(back_populates="projects")
    outputs: Mapped[list[Output]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


Index("ix_projects_owner_updated", Project.owner_id, Project.updated_at.desc())


class Output(Base):
    """One rendered file. A project has many: up to 3 reels, up to 3 YouTube
    compilations, plus manual exports."""

    __tablename__ = "outputs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # reel | youtube | export
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    r2_key: Mapped[str] = mapped_column(String(512), nullable=False)
    srt_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    duration_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)

    project: Mapped[Project] = relationship(back_populates="outputs")


Index("ix_outputs_project", Output.project_id, Output.created_at.desc())


class Job(Base):
    """Durable queue row.

    The desktop build kept jobs in a Python dict, which is correct for one
    process that owns the whole machine and wrong for anything else: a
    redeploy loses every in-flight job, and a second instance cannot see the
    first one's work at all. State lives here so any worker can claim it and
    a restart resumes rather than forgets.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    project_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )

    # Lower runs first. Derived from expected DURATION, not urgency, so a
    # 3-second call never queues behind a 20-minute render (see jobs.PRIORITY).
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    # Which finite resource this kind exhausts — the real concurrency bound
    # (see jobs.LANES). Recorded on the row so a worker can count what is
    # running without re-deriving it from `kind`.
    lane: Mapped[str] = mapped_column(String(16), nullable=False, default="net", server_default="net")

    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    stage: Mapped[str] = mapped_column(String(48), nullable=False, default="", server_default="")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Set for kinds that cost money per attempt (LLM, STT). A retry there is
    # a second bill for a result the first attempt may already have produced.
    no_retry: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Unique when set: two clicks on "transcribe" must not run the same work
    # twice against the same output keys.
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)

    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A running row is only evidence of life while this keeps moving. A
    # worker killed mid-job leaves a corpse in `running` forever otherwise,
    # and that corpse holds its lane slot shut.
    heartbeat_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


Index("ix_jobs_claimable", Job.state, Job.priority, Job.created_at)
Index("ix_jobs_project", Job.project_id, Job.created_at.desc())


class Setting(Base):
    """Operator-editable settings that are NOT secrets.

    Credentials stay in the environment. The desktop build let an HTTP
    handler rewrite its own `.env`; on a public URL that is a credential
    takeover, so the write path for secrets simply does not exist here.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


class SubtitleTemplate(Base):
    """A named subtitle style the whole studio can apply.

    Studio-wide rather than per user, for the same reason the brand assets
    are: a house style that each producer keeps their own copy of stops being
    a house style the first time two of them drift.

    The style is stored as the SubtitleStyle document rather than as columns.
    A template is written rarely and read whole, never queried by font size,
    and a column per field would need a migration every time that model gains
    one — which it just did, and will again.
    """

    __tablename__ = "subtitle_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    style: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    user: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    method: Mapped[str] = mapped_column(String(8), nullable=False, default="", server_default="")
    path: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


Index("ix_audit_at", AuditLog.at.desc())


def utcnow_col():
    return mapped_column(DateTime(timezone=True), server_default=func.now())
