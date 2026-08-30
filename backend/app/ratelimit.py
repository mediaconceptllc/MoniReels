"""In-process rate limiting for the login route.

Password verification is PBKDF2 with 200k rounds — roughly 100ms of CPU per
attempt. Unbounded, a few dozen concurrent guesses saturate every worker and
the whole API stops answering, so this is availability protection first and
guess-slowing second.

Two independent buckets:

* per IP — bounds the CPU one source can spend.
* per (username, IP) — bounds guessing at one account. The username is ALWAYS
  paired with an IP; keyed on username alone, an attacker could lock any
  account they can name just by failing against it.

Counters live in this process, so each instance enforces its own share. That
is a deliberate trade: it needs no Redis and still cuts the achievable rate
by orders of magnitude.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

IP_MAX_ATTEMPTS = 30
IP_WINDOW_SEC = 60.0

USER_MAX_ATTEMPTS = 8
USER_WINDOW_SEC = 300.0

_lock = threading.Lock()
_ip_hits: dict[str, deque[float]] = defaultdict(deque)
_user_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def _prune(hits: deque[float], window: float, now: float) -> None:
    while hits and now - hits[0] > window:
        hits.popleft()


def login_allowed(username: str, ip: str) -> bool:
    """Record an attempt and report whether it may proceed."""
    now = time.time()
    with _lock:
        ip_hits = _ip_hits[ip]
        _prune(ip_hits, IP_WINDOW_SEC, now)
        if len(ip_hits) >= IP_MAX_ATTEMPTS:
            return False

        user_key = (username.lower(), ip)
        user_hits = _user_hits[user_key]
        _prune(user_hits, USER_WINDOW_SEC, now)
        if len(user_hits) >= USER_MAX_ATTEMPTS:
            return False

        ip_hits.append(now)
        user_hits.append(now)
        return True


def login_succeeded(username: str, ip: str) -> None:
    """Clear the per-account counter so a legitimate user who mistyped a few
    times is not locked out for the rest of the window."""
    with _lock:
        _user_hits.pop((username.lower(), ip), None)


def reset() -> None:
    with _lock:
        _ip_hits.clear()
        _user_hits.clear()
