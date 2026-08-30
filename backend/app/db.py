"""SQLAlchemy engine + session factory.

Railway's Postgres hands out a `postgres://` URL; SQLAlchemy 2 only accepts
`postgresql://`, so the scheme is normalized here rather than in every
caller. `pool_pre_ping` is not optional on a managed database: idle
connections are reaped by the provider, and without it the first query after
an idle stretch fails with a stale-connection error instead of reconnecting.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def normalize_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        _engine = create_engine(
            normalize_url(settings.database_url),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaction boundary for worker code and scripts. Commits on clean
    exit, rolls back on any exception, and always closes."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency. Never commits for the caller — a handler that
    changed something says so explicitly, so a read path can't accidentally
    persist a half-built object it only meant to inspect."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
