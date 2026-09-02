"""The forced cut — one env var decides whether two loops terminate.

Both chunkers fall back to cutting audio at a fixed ceiling when no pause
arrives in time, and both advance their cursor by `max_chunk_sec -
overlap_sec`. The overlap is a constant; the ceiling is
DUUDLAGA_MAX_AUDIO_SEC. Set that at or below the overlap and the cursor stops
moving while the loop keeps appending: an infinite loop that also eats memory,
inside a worker thread, so the job's heartbeat keeps ticking and the reaper
never touches it. The same shape as the split_span hang, and the same absence
of any signal — no error, no failed job, nothing in the logs.

Two guards, tested here together because they are one defect: the setting is
bounded where it enters the process, and the arithmetic refuses the case
where it is passed in directly.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.audio.vad_chunking import group_vad_segments_into_chunks
from app.config import Settings
from app.stt.chunking import (
    FORCED_CUT_OVERLAP_SEC,
    compute_pause_boundaries,
    forced_cut_step,
)
from tests.conftest import must_finish

# The chain the ceiling travels: Settings -> DuudlagaConfig.max_audio_sec ->
# client.max_audio_sec -> both loops below.
CHAIN_END = "duudlaga_max_audio_sec"


# --------------------------------------------------------------------------
# The arithmetic itself.
# --------------------------------------------------------------------------


def test_step_is_the_ceiling_less_the_overlap():
    assert forced_cut_step(30.0, FORCED_CUT_OVERLAP_SEC) == pytest.approx(29.6)


@pytest.mark.parametrize("ceiling", [0.4, 0.2, 0.0])
def test_a_ceiling_that_cannot_advance_is_refused(ceiling: float):
    """Raising is the point. A misconfiguration that stops a worker dead with
    no error is the worst outcome available; a job that fails naming the
    variable is the best one."""
    with pytest.raises(ValueError, match="DUUDLAGA_MAX_AUDIO_SEC"):
        forced_cut_step(ceiling, FORCED_CUT_OVERLAP_SEC)


# --------------------------------------------------------------------------
# The two loops. Each would spin forever, so each needs the alarm.
# --------------------------------------------------------------------------


# A shorter alarm than the default on purpose: an unguarded loop appends a
# list entry per iteration, so every extra second of spinning is hundreds of
# megabytes on whatever runs this.
HANG_ALARM_S = 2


def test_pause_boundaries_refuse_a_standstill_instead_of_spinning():
    with must_finish("compute_pause_boundaries", HANG_ALARM_S), pytest.raises(ValueError):
        compute_pause_boundaries(600.0, silences=[], max_chunk_sec=FORCED_CUT_OVERLAP_SEC)


def test_vad_grouping_refuses_a_standstill_instead_of_spinning():
    with must_finish("group_vad_segments_into_chunks", HANG_ALARM_S), pytest.raises(ValueError):
        group_vad_segments_into_chunks(
            [(0.0, 600.0)], max_chunk_sec=FORCED_CUT_OVERLAP_SEC, min_chunk_sec=0.1
        )


def test_a_workable_ceiling_still_force_cuts_the_whole_span():
    """The guard must not change what a sane setting does: a 600s pause-free
    stretch at a 30s ceiling still comes back fully covered and bounded."""
    with must_finish("compute_pause_boundaries"):
        chunks = compute_pause_boundaries(600.0, silences=[], max_chunk_sec=30.0)
    assert chunks[0][0] == 0.0
    assert chunks[-1][1] == 600.0
    assert all(end - start <= 30.0 + 1e-9 for start, end in chunks)


# --------------------------------------------------------------------------
# The outer guard: the value never reaches those loops in the first place.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "-1", "4", "601"])
def test_the_setting_refuses_a_value_the_loops_cannot_use(bad: str, monkeypatch):
    monkeypatch.setenv("DUUDLAGA_MAX_AUDIO_SEC", bad)
    with pytest.raises(ValidationError):
        Settings()


def test_the_floor_stays_above_the_overlap():
    """The bound is only worth having if it actually excludes the standstill.
    Read from the model rather than repeated as a literal, so raising the
    overlap without raising the floor fails here instead of in production."""
    floor = min(
        m.ge for m in Settings.model_fields[CHAIN_END].metadata if hasattr(m, "ge")
    )
    assert floor > FORCED_CUT_OVERLAP_SEC
    forced_cut_step(floor, FORCED_CUT_OVERLAP_SEC)


def test_the_default_is_unchanged(monkeypatch):
    monkeypatch.delenv("DUUDLAGA_MAX_AUDIO_SEC", raising=False)
    assert Settings().duudlaga_max_audio_sec == 30
