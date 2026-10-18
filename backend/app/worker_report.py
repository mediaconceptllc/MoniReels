"""What the worker says it can do — read by the API, which cannot look.

The two services run different images: the worker may be built with the
clean-dub stack (INSTALL_DUB=1) and the API never is. So "can this export
remove the source speech?" has an answer only inside the worker, and the
page that offers the choice runs in the API. Rather than let the producer
find out from a failed export, the worker writes what it can do into the
database and the API reads it.

One row in the `settings` table, written by the worker's housekeeping (so
within a minute of every start, and refreshed after) — never by an HTTP
request, and never among the fields an admin can edit
(app.provider_settings.EDITABLE).

The worker's free disk travels the same way, for the same reason. The queue
page used to print the API's own disk under "why did the export fail", and
the API renders nothing: the two services do not share a filesystem, so its
number was always the wrong machine's.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.dbmodels import Setting

KEY = "worker_report"


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


DISK_FIELDS = ("free_bytes", "total_bytes", "min_free_bytes")


def current() -> WorkerReport:
    from app.audio import dub_bed
    from app.config import get_settings

    return WorkerReport(
        separation=dub_bed.available(),
        model=get_settings().demucs_model,
        at=time.time(),
        disk=_measure_disk(),
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


def publish(db: Session, report: WorkerReport) -> None:
    row = db.get(Setting, KEY)
    value = json.dumps(asdict(report))
    if row is None:
        db.add(Setting(key=KEY, value=value, updated_at=report.at))
    else:
        row.value = value
        row.updated_at = report.at


def read(db: Session) -> WorkerReport | None:
    """The last report, or None when no worker running this code has
    started yet — which is "unknown", and treated as "cannot"."""
    row = db.get(Setting, KEY)
    if row is None or not row.value:
        return None
    try:
        data = json.loads(row.value)
        return WorkerReport(
            separation=bool(data["separation"]),
            model=str(data["model"]),
            at=float(data["at"]),
            disk=_read_disk(data.get("disk")),
        )
    except (ValueError, KeyError, TypeError):
        return None


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
