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

A third bucket guards the password change, per account: it runs the same
PBKDF2 to check the current password, and it was unbounded (15 wrong guesses
in a row, no 429). Keyed on the signed-in account alone — only someone
holding that account's token can spend it, so there is no one else to lock
out.

Counters live in this process, so each instance enforces its own share. That
is a deliberate trade: it needs no Redis and still cuts the achievable rate
by orders of magnitude. Keys whose window has passed are swept, so the maps
hold only recent callers rather than every address that ever tried.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

IP_MAX_ATTEMPTS = 30
IP_WINDOW_SEC = 60.0

USER_MAX_ATTEMPTS = 8
USER_WINDOW_SEC = 300.0

PASSWORD_MAX_ATTEMPTS = 8
PASSWORD_WINDOW_SEC = 300.0

# How often the maps are swept of keys whose window has passed.
SWEEP_EVERY_SEC = 60.0

_lock = threading.Lock()
_ip_hits: dict[str, deque[float]] = defaultdict(deque)
_user_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_password_hits: dict[str, deque[float]] = defaultdict(deque)
_last_sweep = 0.0


def _prune(hits: deque[float], window: float, now: float) -> None:
    while hits and now - hits[0] > window:
        hits.popleft()


def _sweep(now: float) -> None:
    """Drop keys with nothing left in their window. Without this every
    address and name ever tried stayed in memory for the life of the process
    (3,000 keys after a burst, measured, none of them ever removed)."""
    global _last_sweep
    if now - _last_sweep < SWEEP_EVERY_SEC:
        return
    _last_sweep = now
    # Keyed by address, by (name, address) and by user: one loop, so the
    # tables are typed by what they share — a deque of hit times per key.
    tables: tuple[tuple[dict[Any, deque[float]], float], ...] = (
        (_ip_hits, IP_WINDOW_SEC),
        (_user_hits, USER_WINDOW_SEC),
        (_password_hits, PASSWORD_WINDOW_SEC),
    )
    for hits_by_key, window in tables:
        for key in list(hits_by_key):
            _prune(hits_by_key[key], window, now)
            if not hits_by_key[key]:
                del hits_by_key[key]


def login_allowed(username: str, ip: str) -> bool:
    """Record an attempt and report whether it may proceed."""
    now = time.time()
    with _lock:
        _sweep(now)
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


def password_change_allowed(user_id: str) -> bool:
    """Record a password-change attempt for this account and report whether
    it may proceed."""
    now = time.time()
    with _lock:
        _sweep(now)
        hits = _password_hits[user_id]
        _prune(hits, PASSWORD_WINDOW_SEC, now)
        if len(hits) >= PASSWORD_MAX_ATTEMPTS:
            return False
        hits.append(now)
        return True


def password_change_succeeded(user_id: str) -> None:
    with _lock:
        _password_hits.pop(user_id, None)


def tracked_keys() -> int:
    """How many keys the limiter is holding — for tests and diagnostics."""
    with _lock:
        return len(_ip_hits) + len(_user_hits) + len(_password_hits)


def reset() -> None:
    global _last_sweep
    with _lock:
        _ip_hits.clear()
        _user_hits.clear()
        _password_hits.clear()
        _last_sweep = 0.0
