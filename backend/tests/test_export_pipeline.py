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


# --------------------------------------------------------------------------
# The Mongolian voice-over reaches the clips that speak
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_idea_is_rendered_with_the_voice(monkeypatch, tmp_path):
    seen: list = []

    async def fake_render_timeline(handle, binaries, clips, transition, **kwargs):
        seen.append(kwargs.get("voice"))
        return kwargs["output_path"]

    monkeypatch.setattr("app.export.pipeline.render_timeline", fake_render_timeline)
    voice = object()
    await render_all_ideas(
        _handle(), binaries=object(), video_path="C:/video.mp4",
        suggestions=Suggestions(shorts=[_short("a", (0, 5), (6, 10), (11, 20))],
                                youtube=[_youtube("y", [(0, 300)])]),
        transition=Transition(), crf=20, preset="medium", orientation="landscape",
        portrait_fill="blur", supported_xfade=[], container="mp4", output_dir=tmp_path,
        job_id="job-test", voice=voice,
    )
    assert seen == [voice, voice]


@pytest.mark.asyncio
async def test_only_the_content_speaks_and_it_carries_the_level_asked(monkeypatch, tmp_path):
    """A brand intro is not a range of the source and has no lines in it."""
    from app.export import pipeline
    from app.timeline.models import Clip

    cuts: list[tuple[str, object, float]] = []

    async def fake_probe(ffprobe, path):
        return {"fps": 30.0, "has_audio": True, "duration": 3.0}

    async def fake_cut(binaries, handle, clip, has_audio, *args, voice_path=None, original_volume=1.0):
        cuts.append((clip.source_path, voice_path, original_volume))

    async def fake_concat(binaries, handle, paths, total, workdir, out_path):
        Path(out_path).write_bytes(b"mp4")

    class FakeVoice:
        original_volume = 0.25

        def __init__(self) -> None:
            self.asked: list[tuple[float, float]] = []

        async def track_for(self, start, end, out_path):
            self.asked.append((start, end))
            out_path.write_bytes(b"wav")
            return out_path

    monkeypatch.setattr(pipeline, "probe_video", fake_probe)
    monkeypatch.setattr(pipeline, "_cut_and_normalize_clip", fake_cut)
    monkeypatch.setattr(pipeline, "_join_concat", fake_concat)
    intro = tmp_path / "intro.mp4"
    intro.write_bytes(b"x")
    voice = FakeVoice()

    await pipeline.render_timeline(
        _handle(), type("Bin", (), {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"})(),
        # No transition: two clips then join by concatenation, which is faked.
        [Clip(id="c", source_path="/src.mp4", start=5.0, end=9.0, order=0)], Transition(duration=0.0),
        crf=20, preset="medium", orientation="landscape", portrait_fill="pad",
        supported_xfade=[], workdir=tmp_path / "work", output_path=tmp_path / "out.mp4",
        transcript_source="/src.mp4", intro_path=intro, voice=voice,
    )

    assert voice.asked == [(5.0, 9.0)]
    (intro_cut, content_cut) = cuts
    assert intro_cut[0] == str(intro) and intro_cut[1] is None
    assert content_cut[0] == "/src.mp4" and content_cut[1] is not None and content_cut[2] == 0.25
