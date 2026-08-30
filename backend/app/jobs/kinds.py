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
# resource does this exhaust", not from taste.
# ---------------------------------------------------------------------------
LANE_HEAVY = "heavy"      # CPU/RAM in full: ffmpeg, Demucs. Always 1.
LANE_METERED = "metered"  # a paid external service with its own rate limit.
LANE_NET = "net"          # HTTP wait only; cheap to overlap.

LANE_LIMITS = {
    LANE_HEAVY: 1,
    LANE_METERED: 1,
    LANE_NET: 3,
}

KINDS = (
    "import_video",
    "transcribe",
    "suggest",
    "export_all",
    "export",
)

PRIORITY = {
    "import_video": QUICK,
    "suggest": MINUTES,
    "transcribe": VERY_LONG,
    "export": VERY_LONG,
    "export_all": VERY_LONG,
}

LANES = {
    "import_video": LANE_HEAVY,
    "transcribe": LANE_METERED,
    "suggest": LANE_METERED,
    "export": LANE_HEAVY,
    "export_all": LANE_HEAVY,
}

# Kinds where one attempt is one bill. A retry is a second charge for work
# the first attempt may already have completed remotely, so these fail
# visibly and let a human decide.
NO_RETRY = frozenset({"transcribe", "suggest"})

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
