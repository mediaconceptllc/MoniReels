"""Subtitle cues.

duudlaga.dev returns Mongolian ASR text with no terminal punctuation, so the
sentence split in app.stt.chunking finds nothing and one transcript segment is
one whole audio chunk — measured on a real 17:44 transcript, 32 of 68 segments
over 15s and the longest at the full 30s. Rendered straight to a subtitle that
is a wall of text parked on screen for half a minute.
"""
from __future__ import annotations

from app.models import Segment, SubtitleStyle
from app.subtitle.ass import build_ass_document
from app.subtitle.cues import MAX_CUE_CHARS, MAX_CUE_SEC, MAX_LINE_CHARS, to_cues
from app.subtitle.srt import segments_to_srt

# 30s of unpunctuated Mongolian, the shape that reaches this code in production.
_LONG_TEXT = (
    "тэр үед бид бүгд маш их гайхаж байсан учир нь тэр хүн огт өөр зүйл ярьж эхэлсэн "
    "бөгөөд түүний хэлсэн үг бидний бүх төлөвлөгөөг өөрчилсөн юм тэгээд бид дараагийн "
    "өдөр нь уулзаж энэ талаар дэлгэрэнгүй ярилцахаар шийдсэн билээ"
)


def _long_segment() -> Segment:
    return Segment(id="0", start=100.0, end=130.0, text=_LONG_TEXT)


def test_a_thirty_second_block_becomes_readable_cues():
    cues = to_cues([_long_segment()])

    assert len(cues) > 1
    for cue in cues:
        assert cue.end - cue.start <= MAX_CUE_SEC + 1e-9
        assert len(cue.text.replace("\n", " ")) <= MAX_CUE_CHARS


def test_splitting_keeps_the_original_span_and_every_word():
    cues = to_cues([_long_segment()])

    assert cues[0].start == 100.0
    assert cues[-1].end == 130.0
    for earlier, later in zip(cues, cues[1:], strict=False):
        assert earlier.end == later.start  # no gaps, no overlaps
    assert " ".join(c.text.replace("\n", " ") for c in cues) == _LONG_TEXT


def test_no_cue_line_runs_past_the_frame():
    for cue in to_cues([_long_segment()]):
        lines = cue.text.split("\n")
        assert len(lines) <= 2
        assert all(len(line) <= MAX_LINE_CHARS for line in lines)


def test_a_cue_wraps_into_balanced_lines_not_a_filled_one():
    # Filling to 42 would leave a long line over a short one; even halves read
    # better and keep the eye off the ragged edge.
    text = " ".join(["үг"] * 20)  # 59 chars, needs two lines
    (cue,) = to_cues([Segment(id="0", start=0.0, end=4.0, text=text)])

    top, bottom = cue.text.split("\n")
    assert abs(len(top) - len(bottom)) <= 3


def test_a_segment_already_within_the_limits_is_left_alone():
    seg = Segment(id="keep-me", start=0.0, end=3.0, text="богино мөр")
    (cue,) = to_cues([seg])

    assert cue.id == "keep-me"  # its identity and word timings survive
    assert cue.text == "богино мөр"


def test_an_empty_segment_is_dropped():
    assert to_cues([Segment(id="0", start=0.0, end=3.0, text="   ")]) == []


def test_a_single_unsplittable_word_is_never_lost():
    word = "а" * 200
    (cue,) = to_cues([Segment(id="0", start=0.0, end=30.0, text=word)])
    assert cue.text == word


def test_the_speaker_carries_onto_every_piece():
    seg = _long_segment().model_copy(update={"speaker": "SPEAKER_01"})
    assert all(c.speaker == "SPEAKER_01" for c in to_cues([seg]))


# --------------------------------------------------------------------------
# The split belongs to the renderers, not their callers: these two are every
# subtitle this system emits and must not be able to disagree.
# --------------------------------------------------------------------------


def test_srt_splits_a_long_segment_without_being_asked():
    srt = segments_to_srt([_long_segment()])

    assert srt.count("-->") > 1
    # The 30s span is gone: no single cue may still hold the whole block.
    assert "00:01:40,000 --> 00:02:10,000" not in srt


def test_burned_in_subtitles_split_the_same_way():
    ass = build_ass_document([_long_segment()], SubtitleStyle())
    dialogue = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]

    assert len(dialogue) == len(to_cues([_long_segment()]))
    assert all(r"\N" in ln or len(ln) < 200 for ln in dialogue)


def test_the_two_renderers_agree_on_cue_count():
    segs = [_long_segment()]
    srt_cues = segments_to_srt(segs).count("-->")
    ass_cues = len([ln for ln in build_ass_document(segs, SubtitleStyle()).splitlines()
                    if ln.startswith("Dialogue:")])
    assert srt_cues == ass_cues
