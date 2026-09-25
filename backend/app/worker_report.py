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


def current() -> WorkerReport:
    from app.audio import dub_bed
    from app.config import get_settings

    return WorkerReport(
        separation=dub_bed.available(),
        model=get_settings().demucs_model,
        at=time.time(),
    )


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
            separation=bool(data["separation"]), model=str(data["model"]), at=float(data["at"])
        )
    except (ValueError, KeyError, TypeError):
        return None
