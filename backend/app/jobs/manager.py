"""In-memory asyncio job registry: create, run in background, report progress, cancel.

Every long-running backend operation (transcribe, suggest, preview, export)
goes through here so HTTP handlers can return {"job_id": ...} immediately and
never block for more than the time it takes to schedule a task.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.jobs.models import Job, JobState
from app.utils.logging import get_logger

logger = get_logger(__name__)


class JobCancelled(Exception):
    pass


class JobHandle:
    """Passed into a job's worker coroutine to report progress and check cancellation."""

    def __init__(self, manager: JobManager, job: Job) -> None:
        self._manager = manager
        self._job = job
        self.cancel_requested = False
        self._cancel_hook: Callable[[], Awaitable[None]] | None = None

    @property
    def job_id(self) -> str:
        return self._job.id

    def set_cancel_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Register a callback (e.g. FfmpegRun.cancel) invoked when this job is canceled."""
        self._cancel_hook = hook

    async def set_progress(
        self, progress: float, stage: str | None = None, message: str | None = None
    ) -> None:
        self._manager._update(self._job.id, progress=progress, stage=stage, message=message)

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise JobCancelled()

    async def _request_cancel(self) -> None:
        self.cancel_requested = True
        if self._cancel_hook:
            await self._cancel_hook()


WorkerFunc = Callable[[JobHandle], Awaitable[Any]]


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._handles: dict[str, JobHandle] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._listeners: dict[str, list[asyncio.Queue]] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def start(self, worker: WorkerFunc) -> Job:
        job = Job(id=uuid.uuid4().hex)
        self._jobs[job.id] = job
        handle = JobHandle(self, job)
        self._handles[job.id] = handle
        task = asyncio.create_task(self._run(job.id, handle, worker))
        self._tasks[job.id] = task
        return job

    async def _run(self, job_id: str, handle: JobHandle, worker: WorkerFunc) -> None:
        self._update(job_id, state=JobState.RUNNING, stage="starting")
        try:
            result = await worker(handle)
            if handle.cancel_requested:
                self._update(job_id, state=JobState.CANCELED, message="Canceled by user")
            else:
                self._update(job_id, state=JobState.DONE, progress=1.0, result=result, stage="done")
        except JobCancelled:
            self._update(job_id, state=JobState.CANCELED, message="Canceled by user")
        except asyncio.CancelledError:
            self._update(job_id, state=JobState.CANCELED, message="Canceled by user")
            raise
        except Exception as e:  # noqa: BLE001 - job failures must be captured, not raised into the loop
            logger.exception("Job %s failed", job_id)
            self._update(job_id, state=JobState.FAILED, error=str(e))
        finally:
            self._handles.pop(job_id, None)
            self._tasks.pop(job_id, None)

    async def cancel(self, job_id: str) -> bool:
        handle = self._handles.get(job_id)
        job = self._jobs.get(job_id)
        if handle is None or job is None:
            return False
        if job.state not in (JobState.QUEUED, JobState.RUNNING):
            return False
        await handle._request_cancel()
        return True

    def _update(
        self,
        job_id: str,
        *,
        state: JobState | None = None,
        progress: float | None = None,
        stage: str | None = None,
        message: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        if state is not None:
            job.state = state
        if progress is not None:
            job.progress = progress
        if stage is not None:
            job.stage = stage
        if message is not None:
            job.message = message
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        job.updated_at = time.time()
        self._notify(job_id)

    def _notify(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        for queue in self._listeners.get(job_id, []):
            queue.put_nowait(job.to_dict())

    def subscribe(self, job_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._listeners.setdefault(job_id, []).append(queue)
        job = self._jobs.get(job_id)
        if job is not None:
            queue.put_nowait(job.to_dict())
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        listeners = self._listeners.get(job_id)
        if listeners and queue in listeners:
            listeners.remove(queue)


_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
