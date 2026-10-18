"""The spend ledger, read back. Admin only.

`usage_events` (app.metering) holds what every job attempt bought from a
paid provider. This turns it into the table an operator reconciles against
the providers' invoices: month × provider × kind × user, with totals.

Months are UTC calendar months, because that is what OpenRouter and
ElevenLabs bill by; the response says so rather than leaving a reader to
guess why a job run at 07:00 on the 1st in Ulaanbaatar sits in last month.

Cost is the sum of what is KNOWN, and what is not is counted beside it
(`unpriced_requests`): a NULL price means no rate was configured or the
provider stated none, and folding those requests in as zero would make an
unpriced month look cheaper than it was. A group with no price at all says
`cost_usd: null`.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.dbmodels import UsageEvent, User
from app.security import require_admin

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

MONTHS_DEFAULT = 6
MONTHS_MAX = 36


def window_start(now: datetime, months: int) -> datetime:
    """Midnight UTC on the 1st of the month `months - 1` before `now`'s — so
    `months=1` is this month so far, and every window is whole months."""
    index = now.year * 12 + (now.month - 1) - (months - 1)
    year, month0 = divmod(index, 12)
    return datetime(year, month0 + 1, 1, tzinfo=UTC)


_SUMS = ("requests", "prompt_tokens", "completion_tokens", "audio_seconds", "characters")


def _blank() -> dict:
    return {**{k: 0 for k in _SUMS}, "audio_seconds": 0.0, "cost_usd": None, "unpriced_requests": 0}


def _fold(into: dict, row: dict) -> None:
    for k in _SUMS:
        into[k] += row[k]
    if row["cost_usd"] is not None:
        into["cost_usd"] = (into["cost_usd"] or 0.0) + row["cost_usd"]
    into["unpriced_requests"] += row["unpriced_requests"]


def _settle(total: dict) -> dict:
    # Rounded after summing, not before: the totals are the sum of the rows
    # shown, and a per-row round would let six tiny calls add up to nothing.
    total["audio_seconds"] = round(total["audio_seconds"], 1)
    if total["cost_usd"] is not None:
        total["cost_usd"] = round(total["cost_usd"], 6)
    return total


@router.get("/usage")
def usage_report(
    months: int = Query(MONTHS_DEFAULT, ge=1, le=MONTHS_MAX),
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    since = window_start(datetime.now(UTC), months)
    month = func.to_char(
        func.timezone("UTC", func.to_timestamp(UsageEvent.created_at)), "YYYY-MM"
    ).label("month")
    query = (
        select(
            month,
            UsageEvent.provider,
            UsageEvent.kind,
            UsageEvent.user_id,
            func.max(User.username).label("username"),
            func.sum(UsageEvent.requests).label("requests"),
            func.sum(UsageEvent.prompt_tokens).label("prompt_tokens"),
            func.sum(UsageEvent.completion_tokens).label("completion_tokens"),
            func.sum(UsageEvent.audio_seconds).label("audio_seconds"),
            func.sum(UsageEvent.characters).label("characters"),
            # SUM skips NULLs and is NULL only when every row is: exactly
            # "the known part, or unknown".
            func.sum(UsageEvent.cost_usd).label("cost_usd"),
            func.sum(
                case((UsageEvent.cost_usd.is_(None), UsageEvent.requests), else_=0)
            ).label("unpriced_requests"),
        )
        .select_from(UsageEvent)
        .outerjoin(User, User.id == UsageEvent.user_id)
        .where(UsageEvent.created_at >= since.timestamp())
        .group_by(month, UsageEvent.provider, UsageEvent.kind, UsageEvent.user_id)
        # A total order, so the table does not reshuffle between two loads.
        .order_by(month.desc(), UsageEvent.provider, UsageEvent.kind, func.max(User.username),
                  UsageEvent.user_id)
    )

    rows: list[dict] = []
    by_month: dict[str, dict] = {}
    totals = _blank()
    for r in db.execute(query).mappings():
        row = {
            "month": r["month"],
            "provider": r["provider"],
            "kind": r["kind"],
            "user_id": r["user_id"],
            # NULL when the project was deleted mid-job, or the user since.
            "username": r["username"],
            "requests": int(r["requests"] or 0),
            "prompt_tokens": int(r["prompt_tokens"] or 0),
            "completion_tokens": int(r["completion_tokens"] or 0),
            "audio_seconds": round(float(r["audio_seconds"] or 0.0), 1),
            "characters": int(r["characters"] or 0),
            "cost_usd": None if r["cost_usd"] is None else round(float(r["cost_usd"]), 6),
            "unpriced_requests": int(r["unpriced_requests"] or 0),
        }
        rows.append(row)
        _fold(by_month.setdefault(row["month"], {"month": row["month"], **_blank()}), row)
        _fold(totals, row)

    settings = get_settings()
    return {
        "months": months,
        "since": since.timestamp(),
        "timezone": "UTC",
        "rows": rows,
        "month_totals": [_settle(t) for t in by_month.values()],
        "totals": _settle(totals),
        # What a speech minute or a thousand characters is priced at NOW, so
        # an "unknown" can say why. Rows keep the price of their own day.
        "rates": {
            "duudlaga_usd_per_minute": settings.duudlaga_usd_per_minute,
            "elevenlabs_stt_usd_per_minute": settings.elevenlabs_stt_usd_per_minute,
            "elevenlabs_tts_usd_per_1k_chars": settings.elevenlabs_tts_usd_per_1k_chars,
        },
    }
