"""FastAPI entrypoint for the API service.

Run with `python -m app.main` (or uvicorn against `app.main:app`).

Everything the desktop build did here is gone: picking its own free port,
printing `READY <port>` to stdout for the Flutter shell to read, and shutting
itself down after 60 idle seconds. All three existed to be a well-behaved
child process of a desktop app. Railway supplies `$PORT` and decides the
lifecycle.

The one rule this file exists to hold: **no heavy work runs in the web
process.** Every route either reads the database or queues a job.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.api import routes_admin, routes_auth, routes_jobs, routes_projects
from app.config import get_settings
from app.db import session_scope
from app.dbmodels import User
from app.security import client_ip, hash_password
from app.utils.logging import get_logger, setup_logging
from app.video.capabilities import Capabilities, build_capabilities
from app.video.ffmpeg import discover_ffmpeg

logger = get_logger(__name__)


def _bootstrap_admin() -> None:
    """Create the first admin, and only ever the first.

    Gated on the users table being empty rather than on the username being
    absent: keyed on the name, changing the variable later would quietly
    create a second admin, and rotating it would look like a password reset
    that never happened.
    """
    settings = get_settings()
    if not (settings.bootstrap_admin_username and settings.bootstrap_admin_password):
        return
    with session_scope() as db:
        if db.scalar(select(func.count()).select_from(User)):
            return
        salt, digest = hash_password(settings.bootstrap_admin_password)
        db.add(
            User(
                username=settings.bootstrap_admin_username,
                pw_salt=salt,
                pw_hash=digest,
                role="admin",
            )
        )
        logger.info("Created the bootstrap admin account %r", settings.bootstrap_admin_username)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()

    binaries = discover_ffmpeg()
    app.state.ffmpeg = binaries
    # Probed once: the xfade list costs an ffmpeg invocation, which is far
    # too slow to repeat per request.
    app.state.capabilities = await build_capabilities(binaries.ffmpeg)

    # Migrations run here, before the first request is served: a deployment
    # must never answer against a schema it has not been migrated to. The
    # worker does not do this — one writer avoids a deploy-time race.
    from app.migrate import upgrade_to_head

    upgrade_to_head()

    try:
        _bootstrap_admin()
    except Exception:  # noqa: BLE001 - a bootstrap failure must not stop the API from serving
        logger.exception("Bootstrap admin creation failed")

    logger.info(
        "API ready: env=%s ffmpeg=%s r2=%s",
        settings.environment,
        app.state.capabilities.ffmpeg_available,
        settings.r2_enabled,
    )
    yield


app = FastAPI(title="MoniReels API", version="2.0.0", lifespan=lifespan)


def _configure_cors(application: FastAPI) -> None:
    """Exact origins only.

    The desktop build allowed `*`, which was safe purely because the server
    was bound to loopback. On a public URL a wildcard with credentials is
    rejected by every browser anyway, so it would not even work — it would
    just fail confusingly.
    """
    settings = get_settings()
    origins = settings.cors_origin_list
    if not origins:
        logger.warning(
            "CORS_ORIGINS is empty — the browser client will be blocked. Set it to your Vercel URL."
        )
        return
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


_configure_cors(app)


@app.middleware("http")
async def audit(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if request.url.path not in ("/health", "/"):
        logger.info(
            "%s %s -> %d (%.0fms) ip=%s",
            request.method, request.url.path, response.status_code, elapsed_ms, client_ip(request),
        )
    return response


app.include_router(routes_auth.router)
app.include_router(routes_projects.router)
app.include_router(routes_jobs.router)
app.include_router(routes_admin.router)


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """Liveness for the platform.

    Deliberately does NOT touch the database: a slow database would make the
    platform restart a process that is perfectly capable of serving, and the
    restart takes down whatever else it was doing.
    """
    caps: Capabilities = request.app.state.capabilities
    settings = get_settings()
    return JSONResponse(
        {
            "status": "ok",
            "ffmpeg": caps.ffmpeg_available,
            "ffmpeg_version": caps.ffmpeg_version,
            "storage": settings.r2_enabled,
            "environment": settings.environment,
        }
    )


@app.get("/capabilities")
async def capabilities(request: Request) -> dict:
    caps: Capabilities = request.app.state.capabilities
    return caps.to_dict()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # Railway terminates TLS and forwards X-Forwarded-*; without this
        # uvicorn reports every client as the proxy's own address.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
