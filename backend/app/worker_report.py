"""What the workers say they can do — read by the API, which cannot look.

The two services run different images: the worker may be built with the
clean-dub stack (INSTALL_DUB=1) and the API never is. So "can this export
remove the source speech?" has an answer only inside the worker, and the
page that offers the choice runs in the API. Rather than let the producer
find out from a failed export, the worker writes what it can do into the
database and the API reads it.

Each worker keeps one row in `worker_heartbeats`: its lanes, when it
started, when it last spoke, whether it can separate, its commit and its
disk. Written on the housekeeping cadence (so within a minute of every
start, and refreshed after) — never by an HTTP request — deleted on a clean
shutdown and aged out (`purge`) when a worker dies without one.

It used to be ONE row in `settings` (`worker_report`), last writer wins.
With a render pool and an I/O pool (#64) that answer flipped with whichever
worker spoke last: the I/O pool, built without Demucs, said "cannot" a
minute after the render pool said "can". A render worker still writes that
row, for one release, so an API from before the heartbeats (a rollback)
reads what it always read; `read` falls back to it only when no worker
running this code is alive.

The worker's free disk travels the same way, for the same reason. The queue
page used to print the API's own disk under "why did the export fail", and
the API renders nothing: the two services do not share a filesystem, so its
number was always the wrong machine's.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.dbmodels import Setting, WorkerHeartbeat
from app.jobs.kinds import LANE_HEAVY, LANE_LIMITS

KEY = "worker_report"

# A worker speaks every housekeeping round (app.worker.REAP_INTERVAL_SEC,
# a minute); three missed rounds is a worker that is gone, not one that is
# busy.
STALE_SEC = 180
# How long a dead worker's row is kept, so the admin page can still name the
# worker that stopped before it is purged.
FORGET_SEC = 3600


@dataclass(frozen=True)
class WorkerReport:
    #: Demucs and torch are importable: the export can remove the speech.
    separation: bool
    #: The model it would load.
    model: str
    #: When the worker last said so.
    at: float
    #: The worker's scratch disk, as app.utils.paths.disk_report measures it
    #: there. Optional: a report written before this field existed is still a
    #: valid answer about separation, and must not read as "unknown".
    disk: dict[str, int] | None = None
    #: Which worker said so. None in a row written before the heartbeats —
    #: which is how the API tells that a worker it cannot see in
    #: worker_heartbeats (an older build) is still the one speaking.
    worker: str | None = None
    #: Some render workers can separate and some cannot. An export lands on
    #: whichever claims it first, so the answer is "cannot" — and says why.
    partial: bool = False


@dataclass(frozen=True)
class Identity:
    """Who a worker is, fixed at start: what its heartbeat row says beside
    what it measures each round."""

    id: str
    lanes: dict[str, int]
    started_at: float
    commit: str | None = None

    @classmethod
    def of(cls, worker_id: str, lanes: dict[str, int], started_at: float | None = None) -> Identity:
        # Railway sets this on every deploy from git; elsewhere it is absent
        # and the row says nothing rather than a guess.
        commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip()[:64] or None
        return cls(
            id=worker_id[:128],
            lanes=dict(lanes),
            started_at=time.time() if started_at is None else started_at,
            commit=commit,
        )

    def serves(self, lane: str) -> bool:
        return self.lanes.get(lane, 0) > 0


@dataclass(frozen=True)
class Heartbeat:
    """One worker's row, read back."""

    id: str
    lanes: dict[str, int]
    started_at: float
    seen_at: float
    separation: bool
    model: str
    commit: str | None
    disk: dict[str, int] | None

    def serves(self, lane: str) -> bool:
        return self.lanes.get(lane, 0) > 0

    def live(self, now: float) -> bool:
        return now - self.seen_at < STALE_SEC


DISK_FIELDS = ("free_bytes", "total_bytes", "min_free_bytes")


def current(worker: str | None = None) -> WorkerReport:
    from app.audio import dub_bed
    from app.config import get_settings

    return WorkerReport(
        separation=dub_bed.available(),
        model=get_settings().demucs_model,
        at=time.time(),
        disk=_measure_disk(),
        worker=worker,
    )


def _measure_disk() -> dict[str, int] | None:
    from app.utils.paths import disk_report

    try:
        report = disk_report()
    except OSError:
        # A disk that cannot be measured is left unsaid rather than reported
        # as empty: zero free bytes is a claim, and a loud one.
        return None
    return {name: int(report[name]) for name in DISK_FIELDS}


# ---------------------------------------------------------------------------
# Writing (worker side)
# ---------------------------------------------------------------------------


def beat(db: Session, me: Identity, report: WorkerReport) -> None:
    """This worker's row, written or refreshed. One statement, so two
    rounds of the same worker racing (a slow one and the next) cannot fail
    on the primary key."""
    values = {
        "lanes": me.lanes,
        "started_at": me.started_at,
        "seen_at": report.at,
        "separation": report.separation,
        "model": report.model[:80],
        "commit": me.commit,
        "disk": report.disk,
    }
    db.execute(
        insert(WorkerHeartbeat)
        .values(id=me.id, **values)
        .on_conflict_do_update(index_elements=[WorkerHeartbeat.id], set_=values)
    )
    # The old single row, for an API that predates this table. Only a worker
    # that renders writes it: the question it answers is about exports, and
    # an I/O worker built without Demucs saying "cannot" is the flip this
    # table exists to end.
    if me.serves(LANE_HEAVY):
        publish(db, report)


def forget(db: Session, worker_id: str) -> None:
    """A clean shutdown: gone now, not in three minutes."""
    db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.id == worker_id))


def purge(db: Session, now: float | None = None) -> int:
    """Rows of workers that stopped without saying so, once they are old
    enough that nobody is asking about them."""
    cutoff = (time.time() if now is None else now) - FORGET_SEC
    result = db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.seen_at < cutoff))
    return int(getattr(result, "rowcount", 0) or 0)


def publish(db: Session, report: WorkerReport) -> None:
    row = db.get(Setting, KEY)
    value = json.dumps(asdict(report))
    if row is None:
        db.add(Setting(key=KEY, value=value, updated_at=report.at))
    else:
        row.value = value
        row.updated_at = report.at


# ---------------------------------------------------------------------------
# Reading (API side)
# ---------------------------------------------------------------------------


def heartbeats(db: Session) -> list[Heartbeat]:
    """Every row, live or not, in a stable order."""
    rows = db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.id)).all()
    return [
        Heartbeat(
            id=row.id,
            lanes=_read_lanes(row.lanes),
            started_at=float(row.started_at),
            seen_at=float(row.seen_at),
            separation=bool(row.separation),
            model=row.model or "",
            commit=row.commit,
            disk=_read_disk(row.disk),
        )
        for row in rows
    ]


def read(db: Session, now: float | None = None) -> WorkerReport | None:
    """What the workers that RENDER say — exports run in the heavy lane, so
    theirs is the answer about exports — or None when nothing has said
    anything yet, which is "unknown", and treated as "cannot".

    With no live render worker the old single row answers, exactly as it
    did before the heartbeats: written by an older build still running, or
    the last word of a render worker that has since stopped.
    """
    now = time.time() if now is None else now
    renderers = [b for b in heartbeats(db) if b.live(now) and b.serves(LANE_HEAVY)]
    if renderers:
        return _of_renderers(renderers)
    return read_legacy(db)


def _of_renderers(renderers: list[Heartbeat]) -> WorkerReport:
    able = [b for b in renderers if b.separation]
    # The tightest disk is the one an export can fail on.
    measured = [b for b in renderers if b.disk is not None]
    tightest = min(measured, key=lambda b: (b.disk or {})["free_bytes"]) if measured else None
    newest = max(renderers, key=lambda b: b.seen_at)
    return WorkerReport(
        separation=len(able) == len(renderers),
        model=(able[0] if able else newest).model,
        at=newest.seen_at,
        disk=tightest.disk if tightest is not None else None,
        worker=newest.id if len(renderers) == 1 else None,
        partial=0 < len(able) < len(renderers),
    )


def read_legacy(db: Session) -> WorkerReport | None:
    """The single `worker_report` row, or None."""
    row = db.get(Setting, KEY)
    if row is None or not row.value:
        return None
    try:
        data = json.loads(row.value)
        worker = data.get("worker")
        return WorkerReport(
            separation=bool(data["separation"]),
            model=str(data["model"]),
            at=float(data["at"]),
            disk=_read_disk(data.get("disk")),
            worker=worker if isinstance(worker, str) else None,
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _read_lanes(value: object) -> dict[str, int]:
    """Lanes this code knows, each a count; anything else reads as "not
    served" rather than as a crash on the queue page."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for lane in LANE_LIMITS:
        number = value.get(lane)
        out[lane] = number if isinstance(number, int) and not isinstance(number, bool) and number > 0 else 0
    return out


def _read_disk(value: object) -> dict[str, int] | None:
    """Tolerant where the rest is strict: a malformed disk figure loses the
    disk figure, not the separation answer the export guard depends on."""
    if not isinstance(value, dict):
        return None
    out: dict[str, int] = {}
    for name in DISK_FIELDS:
        number = value.get(name)
        # bool is an int to Python; `true` free bytes is not a measurement.
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return None
        out[name] = number
    return out
