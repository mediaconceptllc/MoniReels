"""Projects: create, upload, edit, and start pipeline stages.

Two things every route here obeys:

* **Ownership is checked in the store**, not per route (app.store.get_row),
  so a route added later cannot forget it and expose another account's work.
* **No route does heavy work.** ffprobe, STT, LLM and ffmpeg all run in the
  worker; a handler's job is to validate, queue, and return a job id.

The routes live in app.api.projects, one module per concern; this puts them
together.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.projects import documents, exports, stages, studio, uploads

router = APIRouter()

# Starlette tries routes in the order they were added, and this is the order
# they had as one module — so which route answers a given path is unchanged.
for part in (uploads, documents, stages, studio, exports):
    router.include_router(part.router)
