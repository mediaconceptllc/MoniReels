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

At rest a secret is sealed with `SETTINGS_ENCRYPTION_KEY` when that variable
is set (app.secretbox), so a dump of this table is not a dump of the paid
accounts. Every change is written to `audit_log` by name — who, when, which
field, never the value.
"""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import secretbox
from app.config import Settings, get_settings
from app.dbmodels import AuditLog, Setting
from app.utils.logging import get_logger

logger = get_logger(__name__)

SECRETS = ("openrouter_api_key", "duudlaga_api_key", "elevenlabs_api_key")
PLAIN = ("openrouter_model", "stt_provider", "elevenlabs_tts_model", "elevenlabs_tts_voice_id")
EDITABLE = SECRETS + PLAIN


def _read(db: Session) -> tuple[dict[str, str], dict[str, str]]:
    """The stored values, opened — and, by name, why any could not be."""
    rows = db.scalars(select(Setting).where(Setting.key.in_(EDITABLE))).all()
    values: dict[str, str] = {}
    problems: dict[str, str] = {}
    for row in rows:
        if not row.value:
            continue
        if row.key not in SECRETS:
            values[row.key] = row.value
            continue
        try:
            values[row.key] = secretbox.unseal(row.value)
        except secretbox.SecretKeyError as e:
            problems[row.key] = str(e)
    return values, problems


def _stored(db: Session) -> dict[str, str]:
    """The stored values a job may use. A sealed key this process cannot open
    stops the job with the reason; it is never sent to a provider, and never
    silently replaced by the environment's value either."""
    values, problems = _read(db)
    if problems:
        names = sorted(problems)
        raise secretbox.SecretKeyError(f"{problems[names[0]]} ({', '.join(names)})")
    return values


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
    # Never raises over an unreadable key: this page is where it gets fixed,
    # by entering the key again.
    stored, unreadable = _read(db)
    env = get_settings()
    out: dict[str, dict] = {}
    for name in EDITABLE:
        if name in unreadable:
            out[name] = {"source": "db", "set": True, "hint": "•" * 8}
            continue
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


def storage_status(db: Session) -> dict:
    """How the stored keys are kept, for the admin page's warning.

    `plaintext` lists the keys sitting in the table unencrypted — with the
    variable set the API seals them at startup, so a non-empty list then
    means it has not restarted since. `unreadable` lists sealed keys this
    process cannot open. Names only, never values.
    """
    rows = db.scalars(select(Setting).where(Setting.key.in_(SECRETS))).all()
    stored = [row for row in rows if row.value]
    _, problems = _read(db)
    try:
        secretbox.check()
        error = None
    except secretbox.SecretKeyError as e:
        error = str(e)
    return {
        "encrypted": secretbox.enabled() and error is None,
        "variable": secretbox.VARIABLE,
        "plaintext": sorted(row.key for row in stored if not secretbox.is_sealed(row.value)),
        "unreadable": sorted(problems),
        # The variable itself is unusable — malformed — rather than a key
        # merely missing for some rows.
        "error": error,
    }


def seal_stored(db: Session) -> int:
    """Seal every stored key under the current first key. Returns how many
    rows changed.

    Run by the API at startup. Idempotent, and safe with several instances
    starting together: the rows are locked, so a second instance waits and
    then finds them already sealed. A malformed variable raises here, so the
    API refuses to start instead of storing keys it can never open.

    A row sealed with a key that is not in the list is left exactly as it is
    and named in the log. Refusing to start over it would also close the
    page that fixes it — entering the key again (`apply` writes over a value
    it cannot open) — and rewriting it would destroy the only copy.
    """
    if not secretbox.enabled():
        return 0
    secretbox.check()
    rows = db.scalars(
        select(Setting).where(Setting.key.in_(SECRETS)).with_for_update()
    ).all()
    changed = 0
    unreadable: list[str] = []
    for row in rows:
        if not row.value or not secretbox.needs_reseal(row.value):
            continue
        try:
            row.value = secretbox.reseal(row.value)
        except secretbox.SecretKeyError:
            unreadable.append(row.key)
            continue
        changed += 1
    db.commit()
    if unreadable:
        logger.warning(
            "Stored provider key(s) %s cannot be opened with %s; jobs that need them "
            "will stop until the key is re-entered on the admin page",
            ", ".join(sorted(unreadable)), secretbox.VARIABLE,
        )
    return changed


def apply(
    db: Session, changes: dict[str, str | None], *, actor: str = "", ip: str = ""
) -> list[str]:
    """Write the fields the client actually sent. Returns the names changed.

    `None` leaves a field alone. An empty string drops the row, which is the
    only way back to the environment value — without it, a mistyped key
    entered once could never be un-entered.

    Each changed field gets an `audit_log` row in the same transaction —
    `PUT /admin/settings/<field>` by `actor` — so "who replaced the key, and
    when" has an answer. The value is never written there.
    """
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise ValueError(f"Энэ тохиргоог эндээс өөрчлөх боломжгүй: {', '.join(unknown)}")

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
        stored = secretbox.seal(value) if name in SECRETS else value
        if row is None:
            db.add(Setting(key=name, value=stored, updated_at=time.time()))
        elif not _same(name, row.value, value):
            row.value = stored
            row.updated_at = time.time()
        else:
            continue
        touched.append(name)

    if touched:
        now = time.time()
        db.add_all(
            AuditLog(
                at=now, user=actor[:64], ip=ip[:64], method="PUT",
                path=f"/admin/settings/{name}", status=200,
            )
            for name in touched
        )
        db.commit()
    return touched


def _same(name: str, current: str, new: str) -> bool:
    """Whether a stored value already says `new`. Compared opened: sealing is
    randomised, so two seals of one key never match byte for byte. A value
    that cannot be opened is never "the same" — writing over it is how an
    operator recovers from a lost encryption key."""
    if name not in SECRETS:
        return current == new
    try:
        return secretbox.unseal(current) == new
    except secretbox.SecretKeyError:
        return False
