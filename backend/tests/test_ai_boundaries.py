"""Where a cut is allowed to begin and end.

The system prompt has asked for "start on the first word of a real sentence"
since it was written, and nothing could ever check it: duudlaga.dev returns
Mongolian ASR text with no terminal punctuation, so there were no sentences
in the transcript to start or end on.
"""
from __future__ import annotations

from app.ai.boundaries import (
    pause_end_indices,
    sentence_end_indices,
    snap_cut,
    unfinished_cuts,
)
from app.ai.prompts import repair_short_dict

# Index 0,2,4 finish a sentence; 1 and 3 do not.
_SEGMENTS = [
    (0.0, 2.0, "Тэр өдөр бид уулзсан."),
    (2.0, 4.0, "Тэгээд ярилцлаа"),
    (4.0, 6.0, "удаан ярилцсан юм."),
    (6.0, 8.0, "Дараа нь"),
    (8.0, 10.0, "бүх зүйл өөрчлөгдсөн."),
]


def test_sentence_ends_are_found_where_the_punctuation_is():
    assert sentence_end_indices(_SEGMENTS) == {0, 2, 4}


def test_an_unpunctuated_transcript_reports_no_sentence_ends():
    # The honest answer. Inventing boundaries would be worse than none:
    # this is every transcript this pipeline had before app.ai.punctuate.
    raw = [(0.0, 2.0, "тэр өдөр бид уулзсан"), (2.0, 4.0, "тэгээд ярилцлаа")]
    assert sentence_end_indices(raw) == set()


def test_a_closing_quote_after_the_full_stop_still_ends_a_sentence():
    assert sentence_end_indices([(0.0, 1.0, 'Тэр "за" гэлээ."')]) == {0}


def test_pauses_mark_the_segments_they_land_on():
    # Measured during transcription and free; the fallback when nothing has
    # restored punctuation.
    assert pause_end_indices(_SEGMENTS, [4.05, 10.0]) == {1, 4}


def test_a_pause_nowhere_near_a_segment_end_marks_nothing():
    assert pause_end_indices(_SEGMENTS, [3.0]) == set()


def test_an_end_snaps_to_the_sentence_it_is_inside():
    ends = sentence_end_indices(_SEGMENTS)
    assert snap_cut(1, 3, _SEGMENTS, sentence_ends=ends, pause_ends=set()) == (1, 2)


def test_a_start_snaps_past_the_previous_sentences_end():
    ends = sentence_end_indices(_SEGMENTS)
    # 4 is a sentence end, so 5 would begin one; from 3 the reachable start
    # is 3 (2 ended a sentence).
    start, _ = snap_cut(3, 4, _SEGMENTS, sentence_ends=ends, pause_ends=set())
    assert start == 3


def test_an_edge_with_no_boundary_within_reach_is_left_alone():
    # Dragging a cut further would swap the content the model chose for
    # something nobody picked.
    assert snap_cut(1, 1, _SEGMENTS, sentence_ends={4}, pause_ends=set()) == (1, 1)


def test_a_start_is_never_dragged_onto_the_opening_of_the_video():
    # Index 0 trivially begins a sentence, so offering it as a target pulls
    # every early cut onto the greeting the prompt spends a line excluding.
    start, _ = snap_cut(2, 4, _SEGMENTS, sentence_ends=set(), pause_ends=set())
    assert start == 2


def test_a_cut_that_already_starts_at_zero_stays_there():
    ends = sentence_end_indices(_SEGMENTS)
    assert snap_cut(0, 3, _SEGMENTS, sentence_ends=ends, pause_ends=set())[0] == 0


def test_punctuation_wins_over_a_pause():
    # A pause is where someone breathed; a full stop is where they finished.
    # The cut needs room: an end may never move in front of its own start,
    # so a one-segment cut has nowhere to go.
    _, end = snap_cut(1, 3, _SEGMENTS, sentence_ends={2}, pause_ends={3})
    assert end == 2


def test_an_end_never_moves_in_front_of_its_own_start():
    assert snap_cut(3, 3, _SEGMENTS, sentence_ends={2}, pause_ends=set()) == (3, 3)


def test_pauses_are_used_when_there_is_no_punctuation_at_all():
    raw = [(float(i), float(i + 1), "үг") for i in range(6)]
    _, end = snap_cut(3, 3, raw, sentence_ends=set(), pause_ends={4})
    assert end == 4


def test_an_end_landing_mid_sentence_is_named():
    # Index 1 DOES begin a sentence (0 ended one), so only the end is wrong.
    short = {"cuts": [{"start_index": 1, "end_index": 3}]}
    problems = unfinished_cuts(short, _SEGMENTS, sentence_end_indices(_SEGMENTS))
    assert problems == ["Cut 1 ends mid-sentence at segment 3."]


def test_a_start_landing_mid_sentence_is_named_too():
    # Index 2 is the second half of the sentence that began at 1.
    short = {"cuts": [{"start_index": 2, "end_index": 2}]}
    problems = unfinished_cuts(short, _SEGMENTS, sentence_end_indices(_SEGMENTS))
    assert problems == ["Cut 1 starts mid-sentence at segment 2."]


def test_nothing_is_flagged_when_no_sentence_information_exists():
    # The rule binds exactly when it can be evaluated. Failing every short
    # over a signal that was never available would take the tool offline.
    short = {"cuts": [{"start_index": 1, "end_index": 3}]}
    assert unfinished_cuts(short, _SEGMENTS, set()) == []


def test_the_repair_snaps_before_it_measures_duration():
    # Snapping moves an edge and changes the total, so the duration repairs
    # have to run on the FINAL indices — the reverse order fixes the length
    # and then breaks it again.
    short = {
        "hook_quote": "Тэгээд ярилцлаа",
        "cuts": [
            {"start_index": 1, "end_index": 3, "role": "hook", "reason": "r"},
            {"start_index": 1, "end_index": 1, "role": "context", "reason": "r"},
            {"start_index": 3, "end_index": 4, "role": "payoff", "reason": "r"},
        ],
    }
    fixed = repair_short_dict(
        short, _SEGMENTS, sentence_end_indices(_SEGMENTS), set()
    )
    assert fixed["cuts"][0]["end_index"] == 2  # snapped off the mid-sentence edge


def test_the_repair_is_unchanged_when_no_boundaries_are_known():
    short = {
        "hook_quote": "Тэгээд ярилцлаа",
        "cuts": [
            {"start_index": 1, "end_index": 3, "role": "hook", "reason": "r"},
            {"start_index": 1, "end_index": 1, "role": "context", "reason": "r"},
            {"start_index": 3, "end_index": 4, "role": "payoff", "reason": "r"},
        ],
    }
    assert repair_short_dict(short, _SEGMENTS)["cuts"] == short["cuts"]
