"""Which ideas an export renders, and which stretches of the source they are.

Its own module, free of ffmpeg, because the API asks it too: the export guard
has to know which lines an export will show before it lets the export start
(app.languages.lines_used), and it must know it from exactly the selection
the worker will render — asked two ways, the guard passes an export whose
subtitles then come out with holes, or refuses one that had none.
"""
from __future__ import annotations

from app.models import Suggestions


def pick_ideas(suggestions: Suggestions, payload: dict) -> tuple[Suggestions, list[str]]:
    """Narrow a project's suggestions to the ones a job was queued for.

    An empty or absent selection means everything — which is what the export
    button did before there was any way to choose, so an old queued job and a
    new one behave identically.

    Anything the selection names that is no longer there is SKIPPED and
    returned for the job's warnings, never guessed at. A job can sit in the
    queue while somebody regenerates the suggestions, and rendering whatever
    now occupies index 2 is the one outcome worth ruling out: the producer
    would get a video they never asked for, with no way to tell.
    """
    picked = payload.get("pick") if isinstance(payload, dict) else None
    if not isinstance(picked, dict):
        return suggestions, []

    skipped: list[str] = []
    short_ids = picked.get("shorts")
    if isinstance(short_ids, list):
        by_id = {s.id: s for s in suggestions.shorts}
        shorts = [by_id[i] for i in short_ids if i in by_id]
        skipped += [f"богино видео «{i}» олдсонгүй" for i in short_ids if i not in by_id]
    else:
        shorts = list(suggestions.shorts)

    wanted = picked.get("youtube")
    if isinstance(wanted, list):
        plans = []
        for entry in wanted:
            index = entry.get("i") if isinstance(entry, dict) else None
            title = entry.get("title") if isinstance(entry, dict) else None
            if not isinstance(index, int) or not 0 <= index < len(suggestions.youtube):
                skipped.append(f"YouTube хураангуй №{index} олдсонгүй")
                continue
            plan = suggestions.youtube[index]
            # The position alone is not identity: pin it to the title the
            # selection was made against.
            if title is not None and plan.title != title:
                skipped.append(f"YouTube хураангуй «{title}» өөрчлөгдсөн тул алгаслаа")
                continue
            plans.append(plan)
    else:
        plans = list(suggestions.youtube)

    return Suggestions(shorts=shorts, youtube=plans), skipped


def idea_ranges(suggestions: Suggestions | None) -> list[tuple[float, float]]:
    """Every stretch of the source these ideas cut: each short's cuts and
    each YouTube plan's kept ranges, in order. Overlaps are left in — a
    stretch two ideas share is still one stretch to translate or read."""
    if suggestions is None:
        return []
    ranges = [(c.start, c.end) for s in suggestions.shorts for c in s.cuts]
    ranges += [(r.start, r.end) for plan in suggestions.youtube for r in plan.ranges]
    return ranges
