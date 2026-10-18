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
    """A brand intro is not a range of the source and has no lines in it —
    nor any speech of the source's to remove."""
    from app.export import pipeline
    from app.timeline.models import Clip

    cuts: list[tuple[str, object, float, object]] = []

    async def fake_probe(ffprobe, path):
        return {"fps": 30.0, "has_audio": True, "duration": 3.0}

    async def fake_cut(binaries, handle, clip, has_audio, *args, voice_path=None, original_volume=1.0,
                       bed_path=None):
        cuts.append((clip.source_path, voice_path, original_volume, bed_path))

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

        def bed_for(self, start, end):
            return Path(f"/beds/{start}-{end}.flac")

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
    assert intro_cut[0] == str(intro) and intro_cut[1] is None and intro_cut[3] is None
    assert content_cut[0] == "/src.mp4" and content_cut[1] is not None and content_cut[2] == 0.25
    # The content is laid over the bed for exactly its own range.
    assert content_cut[3] == Path("/beds/5.0-9.0.flac")


# --------------------------------------------------------------------------
# Memory: a long timeline is joined in batches, and every ffmpeg run carries
# its thread counts. MEASURED at the audit: one xfade graph over 28 clips,
# with ffmpeg's own thread defaults on a large host, peaked at 9.07 GB.
# --------------------------------------------------------------------------


def _durations(n: int) -> list[float]:
    # Uneven on purpose: offsets that never land on a frame boundary.
    return [round(3.0 + (i % 3) * 0.4 + (i % 5) * 0.137, 3) for i in range(n)]


def test_a_short_timeline_is_still_one_pass():
    from app.export.pipeline import XFADE_BATCH, plan_xfade_batches
    from app.transition.filtergraph import plan_transition

    durations = _durations(XFADE_BATCH)
    (batch,) = plan_xfade_batches(durations, plan_transition(durations, 0.5), 30.0)
    assert (batch.first, batch.last, batch.tail) == (0, XFADE_BATCH - 1, None)
    assert batch.head == batch.start_frame == 0


@pytest.mark.parametrize("fps", [30.0, 25.0, 29.97002997])
def test_batches_meet_on_one_frame_and_cover_the_single_pass_exactly(fps):
    """Each boundary is a frame INSIDE a clip, clear of both its transitions:
    the batch before shows that clip up to the frame, the batch after from
    it, so the copy-join has no transition to break and no frame to repeat
    or drop. The batch after starts on exactly the output frame one pass
    over everything would show there."""
    from app.export.pipeline import XFADE_BATCH, _first_frame, plan_xfade_batches
    from app.transition.filtergraph import plan_transition

    durations = _durations(28)
    plan = plan_transition(durations, 0.5)
    batches = plan_xfade_batches(durations, plan, fps)

    assert len(batches) > 1
    assert batches[0].first == 0 and batches[-1].last == 27 and batches[-1].tail is None
    for batch in batches:
        assert batch.last - batch.first + 1 <= XFADE_BATCH
    for before, after in zip(batches, batches[1:], strict=False):
        clip = after.first
        assert before.last == clip and before.tail == after.head
        assert after.start_frame == _first_frame(plan.offsets[clip - 1], fps) + after.head
        # Clear of the transition in, and of the one out.
        assert plan.duration * fps < after.head < (durations[clip] - plan.duration) * fps


def test_a_clip_too_short_to_split_is_passed_over():
    from app.export.pipeline import XFADE_BATCH, plan_xfade_batches
    from app.transition.filtergraph import plan_transition

    durations = [3.0] * 12
    durations[XFADE_BATCH - 1] = 1.1  # 0.1 s clear of two 0.5 s transitions
    batches = plan_xfade_batches(durations, plan_transition(durations, 0.5), 30.0)
    assert XFADE_BATCH - 1 not in [b.first for b in batches[1:]]
    assert batches[0].last == XFADE_BATCH - 2


def test_with_no_clip_long_enough_to_split_it_is_one_pass():
    from app.export.pipeline import plan_xfade_batches
    from app.transition.filtergraph import plan_transition

    durations = [1.05] * 12
    assert len(plan_xfade_batches(durations, plan_transition(durations, 0.5), 30.0)) == 1


async def _record_render(monkeypatch, tmp_path, n_clips: int) -> list[list[str]]:
    from app.export import pipeline
    from app.timeline.models import Clip
    from app.video.ffmpeg import FfmpegRun

    runs: list[list[str]] = []

    async def fake_probe(ffprobe, path):
        return {"fps": 30.0, "has_audio": True, "duration": 60.0}

    async def fake_run(self, ffmpeg, args, *, expected_duration_sec, cwd=None, on_progress=None):
        runs.append(list(args))
        Path(args[-1]).write_bytes(b"media")

    monkeypatch.setattr(pipeline, "probe_video", fake_probe)
    monkeypatch.setattr(FfmpegRun, "run", fake_run)
    monkeypatch.setattr("app.export.presets.heavy_threads", lambda: 23)
    clips = [Clip(id=f"c{i}", source_path="/src.mp4", start=d * i, end=d * i + d, order=i)
             for i, d in enumerate(_durations(n_clips))]
    await pipeline.render_timeline(
        _handle(), type("Bin", (), {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"})(), clips,
        Transition(duration=0.5), crf=20, preset="medium", orientation="portrait",
        portrait_fill="blur", supported_xfade=["fade"], workdir=tmp_path / "work",
        output_path=tmp_path / "out.mp4",
    )
    return runs


@pytest.mark.asyncio
@pytest.mark.parametrize("n_clips", [3, 28])
async def test_every_ffmpeg_run_of_an_export_carries_its_thread_counts(monkeypatch, tmp_path, n_clips):
    """Read off the command lines themselves: a cap that does not reach the
    argv of every run limits nothing. Every picture input gets a decoder cap
    of its own (`-threads` is an input option), every encode gets x264's,
    and no xfade pass holds more inputs than a batch."""
    from app.export.pipeline import XFADE_BATCH

    runs = await _record_render(monkeypatch, tmp_path, n_clips)
    xfades = [a for a in runs if any("xfade=" in x for x in a)]
    assert xfades
    for args in runs:
        if "concat" in args:
            assert "-c" in args and args[args.index("-c") + 1] == "copy"
            continue
        inputs = [i for i, a in enumerate(args) if a == "-i"]
        for i in inputs:
            if args[i - 1] == "-vn":
                continue  # the sound-only pass: no picture decoded at all
            assert args[i - 2] == "-threads", args
        if "libx264" in args:
            encoder_threads = args[args.index("libx264"):]
            assert encoder_threads[encoder_threads.index("-threads") + 1] == "8"
    for args in xfades:
        assert args.count("-i") <= XFADE_BATCH
    assert (len(xfades) > 1) == (n_clips > XFADE_BATCH)


FFMPEG = __import__("shutil").which("ffmpeg")


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
@pytest.mark.asyncio
async def test_a_batched_join_is_the_single_pass_frame_for_frame(monkeypatch, tmp_path):
    """The real thing, both ways, on the same clips: the same frames in the
    same places (each one matched against the single pass — a one-frame
    slip measures ~25 dB on this picture), the same length, and the very
    same sound, which is mixed in one pass either way."""
    import json
    import subprocess

    from app.export import pipeline
    from app.timeline.models import Clip
    from app.video.ffmpeg import FfmpegBinaries

    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=8",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "aac", "-shortest", str(src)],
        check=True,
    )
    clips = [Clip(id=f"c{i}", source_path=str(src), start=round(i * 1.13, 3),
                  end=round(i * 1.13 + 1.2 + (i % 2) * 0.17, 3), order=i) for i in range(5)]
    binaries = FfmpegBinaries(ffmpeg=Path(FFMPEG), ffprobe=Path(FFMPEG).with_name("ffprobe"))

    async def render(name: str, batch: int) -> Path:
        monkeypatch.setattr(pipeline, "XFADE_BATCH", batch)
        out = tmp_path / f"{name}.mp4"
        await pipeline.render_timeline(
            _handle(), binaries, clips, Transition(duration=0.3), crf=23, preset="ultrafast",
            orientation="landscape", portrait_fill="pad", supported_xfade=["fade"],
            workdir=tmp_path / name, output_path=out,
        )
        return out

    one, batched = await render("one", 99), await render("batched", 3)
    durations = [c.end - c.start for c in clips]
    plan = pipeline.plan_transition(durations, 0.3)
    assert len(pipeline.plan_xfade_batches(durations, plan, 30.0, size=3)) == 2

    def streams(path: Path) -> list[dict]:
        out = subprocess.run(
            [str(binaries.ffprobe), "-v", "error", "-count_frames", "-show_entries",
             "stream=codec_type,nb_read_frames,duration", "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
        return json.loads(out)["streams"]

    assert streams(one) == streams(batched)

    def sound(path: Path) -> str:
        return subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a", "-f", "md5", "-"],
                              capture_output=True, text=True, check=True).stdout

    assert sound(one) == sound(batched)

    log = tmp_path / "psnr.log"
    subprocess.run([FFMPEG, "-v", "error", "-i", str(one), "-i", str(batched), "-lavfi",
                    f"[0:v][1:v]psnr=stats_file={log.name}", "-f", "null", "-"], check=True, cwd=tmp_path)
    worst = min(
        float("inf") if (v := dict(kv.split(":") for kv in line.split())["psnr_avg"]) == "inf" else float(v)
        for line in log.read_text().splitlines()
    )
    assert worst > 35, worst
