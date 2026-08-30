"""render_all_ideas orchestration - monkeypatches render_timeline itself (no
real ffmpeg): render_timeline's own correctness is its own concern, this
covers what render_all_ideas is actually responsible for - building the
right clip list per idea (single-clip reels vs multi-clip youtube
compilations), output naming, kind-relative indexing, skipping an idea with
no valid clips instead of crashing the batch, and overall progress.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.export.pipeline import render_all_ideas
from app.models import Cut, KeepRange, ShortIdea, Suggestions, YoutubePlan
from app.timeline.models import Transition


class _Handle:
    """Stand-in for app.jobs.queue.JobHandle.

    The real handle flushes progress to Postgres, which this suite has no
    reason to involve: what is under test is which renders are requested and
    with what arguments, not how a percentage is persisted. It implements the
    exact surface render_all_ideas uses, so a change to that surface breaks
    here rather than silently in production.
    """

    def __init__(self, job_id: str = "job-test") -> None:
        self.job_id = job_id
        self.cancel_requested = False
        self.progress: list[float] = []

    async def set_progress(self, progress: float, stage=None, message=None) -> None:
        self.progress.append(progress)

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            from app.jobs.queue import JobCancelled

            raise JobCancelled()

    def set_cancel_hook(self, hook) -> None:
        self._cancel_hook = hook


def _handle() -> _Handle:
    return _Handle()


def _cut(start: float, end: float, role: str = "context") -> Cut:
    return Cut(start=start, end=end, role=role, reason="r")


def _short(title: str, *ranges: tuple[float, float]) -> ShortIdea:
    """Builds a short with one Cut per (start, end) range - always at least
    a hook first and a payoff last, matching the real validity rules, so
    tests can pass however many ranges they need per idea.
    """
    cuts = [
        _cut(s, e, role="hook" if i == 0 else ("payoff" if i == len(ranges) - 1 else "proof"))
        for i, (s, e) in enumerate(ranges)
    ]
    return ShortIdea(
        id=title, title=title, hook_text="h", hook_quote="q", cuts=cuts,
        caption="d", why_it_works="because",
    )


def _youtube(title: str, ranges: list[tuple[float, float]]) -> YoutubePlan:
    krs = [KeepRange(start=s, end=e) for s, e in ranges]
    return YoutubePlan(title=title, throughline="d", ranges=krs, total_duration=sum(e - s for s, e in ranges))


async def _run(monkeypatch, tmp_path, suggestions, handle=None):
    calls: list[list] = []

    async def fake_render_timeline(handle, binaries, clips, transition, **kwargs):
        calls.append(clips)
        return kwargs["output_path"]

    monkeypatch.setattr("app.export.pipeline.render_timeline", fake_render_timeline)

    results = await render_all_ideas(
        handle or _handle(), binaries=object(), video_path="C:/video.mp4", suggestions=suggestions,
        transition=Transition(), crf=20, preset="medium", orientation="landscape",
        portrait_fill="blur",
        supported_xfade=[], container="mp4", output_dir=tmp_path, job_id="job-test",
    )
    return results, calls


@pytest.mark.asyncio
async def test_render_all_ideas_reels_only(tmp_path, monkeypatch):
    # Each reel is now a multi-cut edit, not a single trim.
    suggestions = Suggestions(
        shorts=[
            _short("A", (0, 10), (20, 30)),
            _short("B", (30, 35), (40, 45)),
            _short("C", (60, 65), (70, 75)),
        ],
        youtube=[],
    )
    results, calls = await _run(monkeypatch, tmp_path, suggestions)

    assert len(calls) == 3
    assert all(len(clips) == 2 for clips in calls)  # each reel carries its 2 cuts
    assert [r["kind"] for r in results] == ["reel", "reel", "reel"]
    assert [Path(r["output_path"]).name for r in results] == ["reel_1_A.mp4", "reel_2_B.mp4", "reel_3_C.mp4"]


@pytest.mark.asyncio
async def test_render_all_ideas_reels_and_youtube(tmp_path, monkeypatch):
    suggestions = Suggestions(
        shorts=[
            _short("A", (0, 10), (20, 30)),
            _short("B", (30, 50)),
            _short("C", (60, 65), (70, 75), (80, 85)),
        ],
        youtube=[
            _youtube("Y1", [(0, 100), (200, 300)]),
            _youtube("Y2", [(400, 500)]),
            _youtube("Y3", [(600, 700), (800, 900), (1000, 1100)]),
        ],
    )
    results, calls = await _run(monkeypatch, tmp_path, suggestions)

    assert len(results) == 6
    assert [r["kind"] for r in results] == ["reel", "reel", "reel", "youtube", "youtube", "youtube"]
    assert [Path(r["output_path"]).name for r in results][3:] == [
        "youtube_1_Y1.mp4", "youtube_2_Y2.mp4", "youtube_3_Y3.mp4",
    ]
    # each idea (reel or youtube) carries exactly as many clips as its cuts/keep-ranges
    assert [len(c) for c in calls] == [2, 1, 3, 2, 1, 3]


@pytest.mark.asyncio
async def test_render_all_ideas_skips_youtube_idea_with_no_valid_clips(tmp_path, monkeypatch):
    suggestions = Suggestions(
        shorts=[_short("A", (0, 20)), _short("B", (30, 50)), _short("C", (60, 90))],
        youtube=[_youtube("Empty", []), _youtube("Y2", [(400, 500)]), _youtube("Y3", [(600, 700)])],
    )
    results, calls = await _run(monkeypatch, tmp_path, suggestions)

    # 3 reels + 2 valid youtube ideas - the empty one is skipped, not fatal
    assert len(results) == 5
    assert len(calls) == 5
    assert "Empty" not in [r["title"] for r in results]


@pytest.mark.asyncio
async def test_render_all_ideas_raises_when_no_ideas_at_all(tmp_path, monkeypatch):
    suggestions = Suggestions(shorts=[], youtube=[])
    with pytest.raises(ValueError, match="No suggested ideas"):
        await _run(monkeypatch, tmp_path, suggestions)


@pytest.mark.asyncio
async def test_render_all_ideas_final_progress_is_one(tmp_path, monkeypatch):
    handle = _handle()
    progresses: list[float] = []
    orig_set_progress = handle.set_progress

    async def tracking_set_progress(progress, stage=None, message=None):
        progresses.append(progress)
        await orig_set_progress(progress, stage=stage, message=message)

    handle.set_progress = tracking_set_progress

    shorts = [_short("A", (0, 20)), _short("B", (30, 50)), _short("C", (60, 90))]
    suggestions = Suggestions(shorts=shorts, youtube=[])
    await _run(monkeypatch, tmp_path, suggestions, handle=handle)

    assert progresses[-1] == 1.0


# --------------------------------------------------------------------------
# Frame rate. A brand intro sits at clips[0] and is not what the render
# should take its timing from.
# --------------------------------------------------------------------------


def test_the_frame_rate_comes_from_the_content_not_the_intro():
    from app.export.pipeline import pick_fps_source
    from app.timeline.models import Clip

    intro = Clip(id="i", source_path="/w/intro.mp4", start=0.0, end=4.0, order=-1)
    content = Clip(id="c", source_path="/w/source.mp4", start=10.0, end=20.0, order=0)

    assert pick_fps_source([intro, content], "/w/source.mp4") is content


def test_with_no_named_source_the_first_clip_still_decides():
    from app.export.pipeline import pick_fps_source
    from app.timeline.models import Clip

    a = Clip(id="a", source_path="/w/source.mp4", start=0.0, end=5.0, order=0)
    b = Clip(id="b", source_path="/w/source.mp4", start=9.0, end=12.0, order=1)

    assert pick_fps_source([a, b], None) is a


def test_a_source_that_is_not_in_the_timeline_falls_back():
    # Never raise over a frame rate: an export must survive a mismatch here.
    from app.export.pipeline import pick_fps_source
    from app.timeline.models import Clip

    only = Clip(id="i", source_path="/w/intro.mp4", start=0.0, end=4.0, order=0)
    assert pick_fps_source([only], "/w/gone.mp4") is only
