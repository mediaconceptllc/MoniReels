"""FastAPI app entrypoint.

Run directly (`python -m app.main`) rather than via the `uvicorn` CLI: this
process must pick a free port itself and print `READY <port>` to stdout so
the Flutter shell can read it back after spawning this as a child process.
"""
from __future__ import annotations

import asyncio
import socket
import sys
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_ai, routes_export, routes_jobs, routes_media, routes_project, routes_stt
from app.config import get_settings
from app.utils.logging import get_logger, setup_logging
from app.video.capabilities import Capabilities, build_capabilities
from app.video.ffmpeg import discover_ffmpeg

logger = get_logger(__name__)

_last_request_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()

    binaries = discover_ffmpeg()
    app.state.ffmpeg_binaries = binaries
    app.state.capabilities = await build_capabilities(binaries.ffmpeg)
    logger.info(
        "Startup: ffmpeg_available=%s xfade=%d encoders=%d hwaccel=%s",
        app.state.capabilities.ffmpeg_available,
        len(app.state.capabilities.xfade_transitions),
        len(app.state.capabilities.encoders_listed),
        app.state.capabilities.working_hwaccel_encoder,
    )

    idle_task: asyncio.Task | None = None
    if settings.managed:
        idle_task = asyncio.create_task(_idle_shutdown_watcher(settings.idle_shutdown_sec))

    # Printed here (ASGI lifespan startup), not before uvicorn.run(): uvicorn
    # only reaches this point after its listen socket is already bound, so a
    # client reading this line is guaranteed the port is actually accepting
    # connections. Printing it earlier (pre-bind) is a real race that makes
    # the very first request after spawn fail intermittently.
    print(f"READY {app.state.startup_port}", flush=True)

    yield

    if idle_task:
        idle_task.cancel()


async def _idle_shutdown_watcher(idle_shutdown_sec: int) -> None:
    """Managed mode: exit the process if no HTTP request arrives for idle_shutdown_sec.

    This is the fallback to a Windows Job Object for making sure the backend
    never survives as an orphan if the Flutter shell is killed without a
    clean shutdown signal.
    """
    while True:
        await asyncio.sleep(5)
        if time.time() - _last_request_time > idle_shutdown_sec:
            logger.warning("Idle timeout (%ss) reached in managed mode; shutting down.", idle_shutdown_sec)
            sys.exit(0)


app = FastAPI(title="AI Video Editor Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _track_activity(request: Request, call_next):
    global _last_request_time
    _last_request_time = time.time()
    return await call_next(request)


app.include_router(routes_jobs.router)
app.include_router(routes_project.router)
app.include_router(routes_stt.router)
app.include_router(routes_ai.router)
app.include_router(routes_export.router)
app.include_router(routes_media.router)


@app.get("/health")
async def health(request: Request) -> dict:
    caps: Capabilities = request.app.state.capabilities
    return {
        "status": "ok",
        "ffmpeg": caps.ffmpeg_available,
        "version": caps.ffmpeg_version,
    }


@app.get("/capabilities")
async def capabilities(request: Request) -> dict:
    caps: Capabilities = request.app.state.capabilities
    return caps.to_dict()


def _find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def main() -> None:
    settings = get_settings()
    port = settings.app_port or _find_free_port(settings.app_host)
    app.state.startup_port = port
    uvicorn.run(app, host=settings.app_host, port=port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
