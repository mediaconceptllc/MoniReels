"""Generic job status/cancel/SSE endpoints — shared by every long-running operation."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.jobs.manager import get_job_manager

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict:
    job = get_job_manager().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job.to_dict()


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    ok = await get_job_manager().cancel(job_id)
    if not ok:
        job = get_job_manager().get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return {"canceled": False, "reason": f"job already {job.state.value}"}
    return {"canceled": True}


@router.get("/{job_id}/events")
async def job_events(job_id: str) -> EventSourceResponse:
    manager = get_job_manager()
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    async def event_generator():
        queue = manager.subscribe(job_id)
        try:
            while True:
                data = await queue.get()
                yield {"event": "job", "data": json.dumps(data)}
                if data["state"] in ("done", "failed", "canceled"):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            manager.unsubscribe(job_id, queue)

    return EventSourceResponse(event_generator())
