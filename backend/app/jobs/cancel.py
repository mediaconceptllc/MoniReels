"""Stopping a canceled job before its next paid request — at the request.

A cancel used to be noticed by ffmpeg alone. Every other stage checked once,
after all of its work was done: canceling a transcription at chunk 7 of 40
let all 40 chunks go out and be billed, and the transcript they bought was
then thrown away because the job had been canceled (REL-7, MEASURED).

So the check sits where money is spent, the way app.metering's counting
does: one gate per provider — `OpenRouterClient._post`, the speech clients'
upload, the voice client's synthesis — each calling `check()` before it
sends. A new paid path is covered by being a call to one of them, not by
somebody remembering a check in its loop. A request already in flight is
left to finish: it is billed whether or not its answer is read.

`watch()` is set once per job by the worker (app.worker._run_job), in that
job's own task, so concurrent jobs never see each other's cancel. Outside a
job — an API request, a test calling a client directly — nothing is watched
and `check()` does nothing.
"""
from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar


class JobCancelled(Exception):
    """The job was asked to stop.

    `detail` says how far it got, when the stage that stopped knows — "7 of
    40 chunks were transcribed and are kept" is what a producer needs in
    order to decide whether to run it again; a bare "canceled" is not.
    """

    def __init__(self, detail: dict | None = None) -> None:
        super().__init__("canceled")
        self.detail = detail


_check: ContextVar[Callable[[], None] | None] = ContextVar("job_cancel_check", default=None)


def watch(check: Callable[[], None]) -> None:
    """Make `check` (it raises JobCancelled once the job is canceled) the
    test every paid gate runs, for the rest of this job's task."""
    _check.set(check)


def check() -> None:
    """Raise JobCancelled if the current job has been canceled."""
    current = _check.get()
    if current is not None:
        current()
