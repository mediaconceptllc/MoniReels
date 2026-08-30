"""Provider credentials an admin can change without a redeploy.

This is deliberately not the thing the desktop build had. That was an HTTP
handler that rewrote the process's own `.env` — on a public URL, a way to
hand the server new credentials and have it use them. Nothing here writes a
file or mutates the environment: the values live in one table, the write
path is admin-only, and the set of fields that can be written is closed.

`openrouter_base_url` is not in that set on purpose. A key plus the freedom
to choose where it is sent is an exfiltration primitive — the endpoint would
let an admin point the model calls at their own host and collect the key on
the first request.

The environment stays the source of truth until a row overrides it, so a
deployment with no rows behaves exactly as it did before this existed, and
clearing a field falls back to the environment rather than to nothing.

A stored secret is never returned. `describe` reports where each value comes
from and its last four characters — enough to tell two keys apart, not
enough to use one.
"""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.dbmodels import Setting

SECRETS = ("openrouter_api_key", "duudlaga_api_key", "elevenlabs_api_key")
PLAIN = ("openrouter_model",)
EDITABLE = SECRETS + PLAIN


def _stored(db: Session) -> dict[str, str]:
    rows = db.scalars(select(Setting).where(Setting.key.in_(EDITABLE))).all()
    return {row.key: row.value for row in rows if row.value}


def effective(db: Session) -> Settings:
    """The environment with any stored overrides on top.

    Returns a copy: the cached environment-only Settings stays untouched, so
    a request that reads it concurrently cannot see a half-applied change.
    """
    overrides = _stored(db)
    base = get_settings()
    return base.model_copy(update=overrides) if overrides else base


def mask(value: str) -> str:
    """Enough to recognise a key, never enough to use it."""
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * 8
    return "•" * 8 + value[-4:]


def describe(db: Session) -> dict[str, dict]:
    stored = _stored(db)
    env = get_settings()
    out: dict[str, dict] = {}
    for name in EDITABLE:
        from_db = stored.get(name, "")
        from_env = getattr(env, name, "") or ""
        value = from_db or from_env
        out[name] = {
            "source": "db" if from_db else ("env" if from_env else "unset"),
            "set": bool(value),
            # A secret is reduced to a hint; the model name is not a secret
            # and is the one field an operator needs to read back in full.
            "hint": mask(value) if name in SECRETS else value,
        }
    return out


def apply(db: Session, changes: dict[str, str | None]) -> list[str]:
    """Write the fields the client actually sent. Returns the names changed.

    `None` leaves a field alone. An empty string drops the row, which is the
    only way back to the environment value — without it, a mistyped key
    entered once could never be un-entered.
    """
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise ValueError(f"not editable: {', '.join(unknown)}")

    touched: list[str] = []
    for name, raw in changes.items():
        if raw is None:
            continue
        value = raw.strip()
        row = db.get(Setting, name)
        if not value:
            if row is not None:
                db.delete(row)
                touched.append(name)
            continue
        if row is None:
            db.add(Setting(key=name, value=value, updated_at=time.time()))
        elif row.value != value:
            row.value = value
            row.updated_at = time.time()
        else:
            continue
        touched.append(name)

    if touched:
        db.commit()
    return touched
