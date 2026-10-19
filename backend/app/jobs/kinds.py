"""Job taxonomy: what kinds exist, how they are ordered, and what each one
exhausts.

Adding a kind means adding it to all four maps below AND registering a
handler in app.worker.HANDLERS. `validate_registry()` is called at worker
startup so a half-registered kind fails loudly instead of sitting in the
queue forever.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Priority — LOWER runs first, derived from expected DURATION, not urgency.
#
# Ordering by "how important does this feel" is what puts a 3-second call
# behind a 20-minute render. Ordering by duration class means the queue
# drains in roughly shortest-job-first order, which minimises how long the
# average job waits. Jobs in the same class share a number and fall back to
# FIFO. Whether a job costs money is NOT an input here — that is NO_RETRY's
# concern.
# ---------------------------------------------------------------------------
SECONDS = 10
QUICK = 20
MINUTES = 40
VERY_LONG = 80

# ---------------------------------------------------------------------------
# Lanes — the real concurrency bound. The number comes from "which finite
# resource does this exhaust", not from taste, and it is counted where that
# resource is: by each WORKER, for the jobs it is running itself (see
# open_lanes and app.worker.main).
#
# It used to be counted across every worker (heavy=1, metered=1 for the whole
# system): a second worker box sat idle beside a render it could have run
# itself, and all paid work shared one slot, so a 30-second suggestion
# waited out another producer's 8-minute transcription (audit ARCH-2).
# ---------------------------------------------------------------------------
LANE_HEAVY = "heavy"  # this box's CPU/RAM/disk in full: ffmpeg, Demucs.
LANE_STT = "stt"      # a speech-to-text provider (duudlaga, ElevenLabs Scribe).
LANE_LLM = "llm"      # the language model (OpenRouter).

# Billed per call by an outside service with its own rate limit. Two lanes,
# not one, because they are two providers: a transcription holding the STT
# provider's concurrency is no reason for a suggestion to wait.
METERED_LANES = frozenset({LANE_STT, LANE_LLM})

# Per worker, when WORKER_LANES does not say otherwise (see parse_lanes).
# heavy is 1: one render saturates the box, and a second makes both slower
# and risks the health check. stt is 1: a transcription is 4 requests in
# flight at once (DUUDLAGA_CONCURRENCY) and a 6 GB scratch budget, the same
# per box as before. llm is 2: a few short calls, no disk.
LANE_LIMITS = {
    LANE_HEAVY: 1,
    LANE_STT: 1,
    LANE_LLM: 2,
}

KINDS = (
    "import_video",
    "transcribe",
    "translate",
    "suggest",
    "export_all",
    "export",
)

PRIORITY = {
    "import_video": QUICK,
    "suggest": MINUTES,
    # A few LLM calls, like suggest — minutes, not the tens a render takes.
    "translate": MINUTES,
    "transcribe": VERY_LONG,
    "export": VERY_LONG,
    "export_all": VERY_LONG,
}

LANES = {
    "import_video": LANE_HEAVY,
    "transcribe": LANE_STT,
    "suggest": LANE_LLM,
    "translate": LANE_LLM,
    "export": LANE_HEAVY,
    "export_all": LANE_HEAVY,
}

# Kinds where one attempt is one bill. A retry is a second charge for work
# the first attempt may already have completed remotely, so these fail
# visibly and let a human decide.
NO_RETRY = frozenset({"transcribe", "translate", "suggest"})

MAX_ATTEMPTS = 3


def priority_of(kind: str) -> int:
    # An unregistered kind sorts LAST rather than first: a mistake should
    # never let unknown work jump the queue.
    return PRIORITY.get(kind, VERY_LONG)


def lane_of(kind: str) -> str:
    # Unknown kinds get the most restrictive lane. Guessing "cheap" for
    # something unrecognised is how one bad kind saturates a box.
    return LANES.get(kind, LANE_HEAVY)


def no_retry(kind: str) -> bool:
    return kind in NO_RETRY


def kinds_in(lanes) -> list[str]:
    """The kinds a worker with room in `lanes` may claim.

    Only kinds this code KNOWS: a row of a kind added by newer code (a
    rollback, or the old worker still draining through a deploy) waits for a
    worker that has its handler, instead of being claimed and failed here.
    """
    return [kind for kind in KINDS if lane_of(kind) in lanes]


def parse_lanes(spec: str) -> dict[str, int]:
    """WORKER_LANES — how many jobs of each lane this worker runs at once.

    "" means LANE_LIMITS. Otherwise `lane=N` pairs, comma-separated, and a
    lane left out is one this worker does not run at all: that is how one
    service becomes a render pool (`heavy=1`) and another an I/O pool
    (`stt=2,llm=4`). Anything unreadable raises — a typo such as `hevy=1`
    must stop the worker at startup, not quietly turn renders off.
    """
    if not spec.strip():
        return dict(LANE_LIMITS)
    caps: dict[str, int] = {}
    for item in spec.split(","):
        name, sep, number = item.strip().partition("=")
        name = name.strip()
        if not sep or name not in LANE_LIMITS:
            raise ValueError(
                f"WORKER_LANES: {item.strip()!r} is not lane=N with a lane among "
                f"{', '.join(LANE_LIMITS)} (got {spec!r})"
            )
        if name in caps:
            raise ValueError(f"WORKER_LANES: {name} is given twice (got {spec!r})")
        try:
            caps[name] = int(number.strip())
        except ValueError:
            raise ValueError(f"WORKER_LANES: {name}={number.strip()!r} is not a whole number") from None
        if caps[name] < 0:
            raise ValueError(f"WORKER_LANES: {name} cannot be negative")
    if not any(caps.values()):
        raise ValueError(f"WORKER_LANES: {spec!r} runs no lane at all; this worker would never take a job")
    return {name: caps.get(name, 0) for name in LANE_LIMITS}


def open_lanes(caps: dict[str, int], busy: list[str], ceiling: int = 0) -> list[str]:
    """The lanes in which a worker running jobs of lanes `busy` may start one
    more. `ceiling` > 0 also bounds the total (WORKER_CONCURRENCY)."""
    if ceiling and len(busy) >= ceiling:
        return []
    return [lane for lane, cap in caps.items() if busy.count(lane) < cap]


def validate_registry(handlers: dict) -> None:
    """Every kind must appear in all four maps and have a handler."""
    problems = []
    for kind in KINDS:
        if kind not in PRIORITY:
            problems.append(f"{kind}: missing from PRIORITY")
        if kind not in LANES:
            problems.append(f"{kind}: missing from LANES")
        if kind not in handlers:
            problems.append(f"{kind}: no handler registered")
    for kind in handlers:
        if kind not in KINDS:
            problems.append(f"{kind}: handler registered for an unknown kind")
    if problems:
        raise RuntimeError("Job registry is inconsistent:\n  " + "\n  ".join(problems))
