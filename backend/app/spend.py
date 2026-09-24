"""What a project has cost, and what the next paid step is likely to cost.

Every paid model call is already metered — app.ai.usage counts it at the one
gate every call passes through, and the worker writes the total into the job's
result. Nothing ever read it back, so the producer could see a bill only by
opening the database.

Two numbers, and they are different in kind:

  * SPENT is exact. It is the sum of what OpenRouter itself charged, read off
    the jobs that produced it.
  * The ESTIMATE is a measurement, not a prediction. It is the median rate
    per 1000 transcript characters across the owner's own completed runs,
    multiplied by this transcript. It arrives with the number of runs behind
    it, because "≈ $0.03 from one run" and "≈ $0.03 from twenty" are not the
    same claim and a screen that shows only the dollars says they are.

What is NOT here is as important. Speech-to-text is billed by duudlaga.dev or
ElevenLabs per minute of audio and NOTHING in this system measures it, so a
transcribe job's cost is unknown rather than zero — `stt_measured` says so out
loud. Reporting 0.0 for it would be the most expensive kind of wrong: the one
that reads as good news.
"""
from __future__ import annotations

from statistics import median
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dbmodels import Job, Project

# Enough runs to have a median worth the name, few enough that one query stays
# cheap. The 30-day prune (config.job_keep_days) is the real bound anyway.
RATE_SAMPLE_LIMIT = 50

# Below this a transcript is too short for its rate to say anything about a
# real one: the prompt's fixed overhead dominates, so the per-character rate
# comes out enormous and scaling it up to a 40-minute video is nonsense.
MIN_RATE_CHARS = 500


def _output(job: Job) -> dict:
    """The handler's return value, or an empty dict.

    `result` holds {"payload": …, "output": …} and either half can be absent:
    a job that failed before its handler returned has no output at all, and
    rows written before a field existed simply lack it.
    """
    result = job.result if isinstance(job.result, dict) else {}
    output = result.get("output")
    return output if isinstance(output, dict) else {}


def job_cost(job: Job) -> float | None:
    """What this job is known to have cost, or None when nothing measured it.

    None and 0.0 are different answers and must not be collapsed: an export
    genuinely costs nothing outside, while a transcribe job spends real money
    that no counter here ever saw.
    """
    llm = _output(job).get("llm")
    if not isinstance(llm, dict):
        return None
    cost = llm.get("cost_usd")
    return float(cost) if isinstance(cost, int | float) else None


def project_spend(db: Session, project_id: str) -> tuple[float, int]:
    """(dollars, number of jobs that reported one) for one project."""
    jobs = db.scalars(select(Job).where(Job.project_id == project_id)).all()
    costs = [c for c in (job_cost(j) for j in jobs) if c is not None]
    return round(sum(costs), 6), len(costs)


class Rate(NamedTuple):
    per_char: float
    samples: int
    #: How many shorts the measured runs were asked for (their median). The
    #: bill follows that count — the model writes out every short it is told
    #: to — so an estimate measured on runs of three says nothing exact about a
    #: request for eight, and the page must be able to say which it was.
    basis_shorts: int


def _asked_shorts(output: dict) -> int | None:
    """What a run was asked for. Rows from before the count existed record
    only what came back — and then what came back WAS what was asked, since
    the answer was always three."""
    for key in ("requested_shorts", "shorts"):
        value = output.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def suggest_rate(db: Session, owner_id: str) -> Rate | None:
    """Dollars per character of transcript, from this owner's own runs.

    Per CHARACTER rather than per run, because the bill is driven by how much
    text goes into the prompt — a 5-minute video and a 50-minute one differ by
    an order of magnitude, which is exactly the range the question "will this
    cost cents or dollars" is asking about. A flat average of past bills would
    answer confidently and be wrong by 10x.

    The median, not the mean: one run against an expensive model would drag an
    average somewhere no future run will go.
    """
    rows = db.scalars(
        select(Job)
        .join(Project, Job.project_id == Project.id)
        .where(
            Project.owner_id == owner_id,
            Job.kind == "suggest",
            Job.state == "done",
        )
        .order_by(Job.created_at.desc())
        .limit(RATE_SAMPLE_LIMIT)
    ).all()

    rates = []
    asked = []
    for job in rows:
        cost = job_cost(job)
        output = _output(job)
        characters = output.get("characters")
        if cost is None or cost <= 0 or not isinstance(characters, int):
            continue
        if characters < MIN_RATE_CHARS:
            continue
        rates.append(cost / characters)
        count = _asked_shorts(output)
        if count:
            asked.append(count)
    if not rates:
        return None
    return Rate(median(rates), len(rates), round(median(asked)) if asked else 0)


def view(db: Session, *, project_id: str, owner_id: str, characters: int, keep_days: int) -> dict:
    """The block the project page reads.

    `keep_days` travels with the numbers because the jobs behind them are
    pruned: a project six weeks old reports far less than it spent, and a
    total presented without that is simply wrong by however much was deleted.
    """
    spent_usd, priced_jobs = project_spend(db, project_id)
    rate = suggest_rate(db, owner_id) if characters else None
    return {
        "spent_usd": spent_usd,
        "priced_jobs": priced_jobs,
        "keep_days": keep_days,
        # Null, not zero: there is nothing to go on until a run has been
        # measured, and a made-up number beside a paid button is worse than
        # no number at all.
        "suggest_estimate_usd": round(rate.per_char * characters, 4) if rate else None,
        "suggest_samples": rate.samples if rate else 0,
        # Null when unknown, never a guessed 3: see Rate.basis_shorts.
        "suggest_basis_shorts": (rate.basis_shorts or None) if rate else None,
        # Speech-to-text spends money that nothing here counts. Said in the
        # payload rather than assumed by the page, so the one place that knows
        # is the one place that says it.
        "stt_measured": False,
    }
