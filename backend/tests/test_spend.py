"""What a project cost, and what the next paid run is likely to cost.

The numbers were all already there — app.ai.usage meters every paid call at
the one gate they pass through, and the worker writes the total into the job's
result. Nothing read them back, so these tests are mostly about the two ways
that reading can lie: collapsing "nothing measured it" into "it was free", and
projecting one run's bill onto a transcript ten times the size.
"""
from __future__ import annotations

import time

import pytest

from app import spend
from app.dbmodels import Job, Project, User
from app.security import hash_password
from tests.conftest import requires_db

pytestmark = requires_db


def _user(db, uid: str, username: str) -> User:
    salt, digest = hash_password("hunter2hunter2")
    user = User(id=uid, username=username, pw_salt=salt, pw_hash=digest)
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def owner(db):
    return _user(db, "spendowner", "spender")


def _project(db, owner, pid: str = "spendproj") -> Project:
    row = Project(id=pid, owner_id=owner.id, name=pid, doc={})
    db.add(row)
    db.commit()
    return row


def _job(db, project, *, kind="suggest", state="done", output=None, jid=None) -> Job:
    job = Job(
        id=jid or f"j{int(time.time() * 1_000_000) % 10**9}{id(output) % 1000}",
        project_id=project.id,
        kind=kind,
        state=state,
        result={"payload": {}, "output": output},
        created_at=time.time(),
    )
    db.add(job)
    db.commit()
    return job


def _llm(cost: float, characters: int | None = 10_000) -> dict:
    out: dict = {"shorts": 3, "youtube": 2, "llm": {"cost_usd": cost, "calls": 2, "models": ["m"]}}
    if characters is not None:
        out["characters"] = characters
    return out


# --------------------------------------------------------------------------
# job_cost — the distinction the whole feature rests on
# --------------------------------------------------------------------------

def test_a_job_that_reported_a_charge_reports_it(db, owner):
    project = _project(db, owner)
    job = _job(db, project, output=_llm(0.0312))
    assert spend.job_cost(job) == pytest.approx(0.0312)


def test_an_unmeasured_job_is_none_and_not_zero(db, owner):
    """A transcribe job spends real money at duudlaga.dev and nothing here
    counts it. Returning 0.0 would put "free" on the screen for the single
    most expensive thing this product does."""
    project = _project(db, owner)
    job = _job(db, project, kind="transcribe", output={"segments": 120})
    assert spend.job_cost(job) is None


def test_a_job_that_never_produced_output_is_none(db, owner):
    project = _project(db, owner)
    job = _job(db, project, state="failed", output=None)
    assert spend.job_cost(job) is None


def test_a_malformed_llm_block_is_none_rather_than_an_exception(db, owner):
    """Rows outlive the code that wrote them. A result shaped differently by
    an older worker must read as "unknown", not crash the project page."""
    project = _project(db, owner)
    for bad in ({"llm": "0.03"}, {"llm": {"cost_usd": "lots"}}, {"llm": {}}, {"llm": None}):
        assert spend.job_cost(_job(db, project, output=bad)) is None


# --------------------------------------------------------------------------
# project_spend
# --------------------------------------------------------------------------

def test_spend_sums_only_the_jobs_that_reported_one(db, owner):
    project = _project(db, owner)
    _job(db, project, output=_llm(0.02))
    _job(db, project, output=_llm(0.01))
    _job(db, project, kind="export", output={"outputs": 3})
    usd, priced = spend.project_spend(db, project.id)
    assert usd == pytest.approx(0.03)
    # Three jobs ran; two of them are known to have cost anything.
    assert priced == 2


def test_another_project_does_not_add_to_this_one(db, owner):
    mine = _project(db, owner, "spendmine")
    theirs = _project(db, owner, "spendtheirs")
    _job(db, mine, output=_llm(0.02))
    _job(db, theirs, output=_llm(5.00))
    assert spend.project_spend(db, mine.id)[0] == pytest.approx(0.02)


def test_a_project_with_no_jobs_spends_nothing_and_says_so(db, owner):
    project = _project(db, owner, "spendempty")
    assert spend.project_spend(db, project.id) == (0.0, 0)


# --------------------------------------------------------------------------
# suggest_rate — the estimate's basis
# --------------------------------------------------------------------------

def test_the_rate_is_per_character_not_per_run(db, owner):
    """The bill follows the transcript into the prompt. A flat average of past
    runs would answer "about 3 cents" for a video ten times longer than every
    run behind that average."""
    project = _project(db, owner)
    _job(db, project, output=_llm(0.03, characters=10_000))
    rate, samples = spend.suggest_rate(db, owner.id)
    assert rate == pytest.approx(0.03 / 10_000)
    assert samples == 1


def test_the_median_is_used_so_one_expensive_run_cannot_set_the_rate(db, owner):
    project = _project(db, owner)
    for cost in (0.01, 0.012, 0.011, 2.50):
        _job(db, project, output=_llm(cost, characters=10_000))
    rate, samples = spend.suggest_rate(db, owner.id)
    assert samples == 4
    # The mean of those four is ~0.63/10k. The median is not dragged there.
    assert rate == pytest.approx(0.0115 / 10_000)


def test_runs_from_the_owners_other_projects_count(db, owner):
    """The first suggestion on a NEW project is exactly when the question is
    asked, and that project has no history of its own."""
    first = _project(db, owner, "spendA")
    second = _project(db, owner, "spendB")
    _job(db, first, output=_llm(0.03, characters=10_000))
    _job(db, second, output=_llm(0.03, characters=10_000))
    assert spend.suggest_rate(db, owner.id)[1] == 2


def test_another_owners_runs_do_not_count(db, owner):
    stranger = _user(db, "spendstranger", "stranger")
    theirs = Project(id="spendstrangerproj", owner_id=stranger.id, name="s", doc={})
    db.add(theirs)
    db.commit()
    _job(db, theirs, output=_llm(0.03, characters=10_000))
    assert spend.suggest_rate(db, owner.id) is None


def test_a_run_without_its_input_size_cannot_set_a_rate(db, owner):
    """Jobs recorded before `characters` existed carry a cost and no scale.
    They are still reported as SPENT; they simply cannot project forward."""
    project = _project(db, owner)
    _job(db, project, output=_llm(0.03, characters=None))
    assert spend.suggest_rate(db, owner.id) is None
    assert spend.project_spend(db, project.id)[0] == pytest.approx(0.03)


def test_a_tiny_transcript_cannot_set_a_rate(db, owner):
    """Below MIN_RATE_CHARS the prompt's fixed overhead IS the bill, so the
    per-character rate comes out enormous — and scaling that to a 40-minute
    video would put a frightening number beside the button."""
    project = _project(db, owner)
    _job(db, project, output=_llm(0.02, characters=40))
    assert spend.suggest_rate(db, owner.id) is None


def test_an_unfinished_or_failed_run_is_not_a_measurement(db, owner):
    project = _project(db, owner)
    _job(db, project, state="failed", output=_llm(0.03))
    _job(db, project, state="running", output=_llm(0.03))
    assert spend.suggest_rate(db, owner.id) is None


def test_a_free_run_is_not_a_rate(db, owner):
    """A zero would drag the median toward zero and promise a free run."""
    project = _project(db, owner)
    _job(db, project, output=_llm(0.0, characters=10_000))
    assert spend.suggest_rate(db, owner.id) is None


def test_only_suggest_runs_set_the_suggest_rate(db, owner):
    project = _project(db, owner)
    _job(db, project, kind="export", output=_llm(0.03, characters=10_000))
    assert spend.suggest_rate(db, owner.id) is None


# --------------------------------------------------------------------------
# view — what the page reads
# --------------------------------------------------------------------------

def test_the_estimate_scales_with_this_projects_transcript(db, owner):
    project = _project(db, owner)
    _job(db, project, output=_llm(0.03, characters=10_000))
    small = spend.view(db, project_id=project.id, owner_id=owner.id, characters=5_000, keep_days=30)
    large = spend.view(db, project_id=project.id, owner_id=owner.id, characters=50_000, keep_days=30)
    assert small["suggest_estimate_usd"] == pytest.approx(0.015)
    assert large["suggest_estimate_usd"] == pytest.approx(0.15)


def test_with_nothing_measured_the_estimate_is_null_not_zero(db, owner):
    """Null is "we do not know"; 0 beside a paid button is a promise."""
    project = _project(db, owner, "spendfresh")
    view = spend.view(db, project_id=project.id, owner_id=owner.id, characters=10_000, keep_days=30)
    assert view["suggest_estimate_usd"] is None
    assert view["suggest_samples"] == 0


def test_a_project_with_no_transcript_gets_no_estimate(db, owner):
    """Nothing can be suggested from a transcript that does not exist, so
    there is no run to price."""
    project = _project(db, owner)
    _job(db, project, output=_llm(0.03, characters=10_000))
    view = spend.view(db, project_id=project.id, owner_id=owner.id, characters=0, keep_days=30)
    assert view["suggest_estimate_usd"] is None


def test_the_view_says_how_many_runs_are_behind_the_estimate(db, owner):
    project = _project(db, owner)
    for _ in range(3):
        _job(db, project, output=_llm(0.03, characters=10_000))
    view = spend.view(db, project_id=project.id, owner_id=owner.id, characters=10_000, keep_days=30)
    assert view["suggest_samples"] == 3


def test_the_view_carries_the_window_its_numbers_came_from(db, owner):
    """The jobs behind these totals are pruned after keep_days, so a total
    without that window is short by however much was deleted."""
    project = _project(db, owner)
    view = spend.view(db, project_id=project.id, owner_id=owner.id, characters=0, keep_days=30)
    assert view["keep_days"] == 30


def test_the_view_states_that_speech_to_text_is_unmeasured(db, owner):
    project = _project(db, owner)
    view = spend.view(db, project_id=project.id, owner_id=owner.id, characters=0, keep_days=30)
    assert view["stt_measured"] is False
