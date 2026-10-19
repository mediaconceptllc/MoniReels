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

import asyncio
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app import observability
from app.api import routes_admin, routes_auth, routes_jobs, routes_projects, routes_usage
from app.bodylimit import BodyLimit
from app.config import Settings, get_settings
from app.db import session_scope
from app.dbmodels import User
from app.security import client_ip, hash_password
from app.utils.logging import get_logger, request_id, setup_logging
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


JWT_SECRET_MIN_BYTES = 32


def startup_problems(settings: Settings) -> list[str]:
    """Configuration this API must not serve with, in words an operator can
    act on — and never the values themselves.

    Refusing to start is louder than a warning on purpose: both of these
    LOOK like a working deployment. A short or empty JWT secret signs and
    accepts tokens normally (an empty one accepts tokens anyone can sign),
    and a wildcard origin works perfectly for the real frontend while
    answering every other site too.
    """
    problems = []
    if len(settings.jwt_secret.encode()) < JWT_SECRET_MIN_BYTES:
        problems.append(
            f"JWT_SECRET is shorter than {JWT_SECRET_MIN_BYTES} bytes (or unset). Generate one with "
            '`python -c "import secrets; print(secrets.token_urlsafe(48))"`. '
            "Changing it signs everyone out once."
        )
    if "*" in settings.cors_origin_list:
        problems.append(
            'CORS_ORIGINS contains "*". List the frontend\'s exact origin(s), comma-separated, '
            "for example https://<your-app>.vercel.app."
        )
    return problems


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()

    problems = startup_problems(settings)
    if problems:
        for problem in problems:
            logger.error("Refusing to start: %s", problem)
        raise RuntimeError("Unsafe configuration: " + " | ".join(problems))

    observability.init_error_reports("api")

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
    # A wildcard never reaches the middleware, even if startup_problems were
    # bypassed: with credentials on, Starlette echoes ANY origin back.
    origins = [o for o in settings.cors_origin_list if o != "*"]
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
        allow_headers=["Authorization", "Content-Type", observability.REQUEST_ID_HEADER],
        # Readable by the page, so a failed request can be quoted back.
        expose_headers=[observability.REQUEST_ID_HEADER],
    )


# Innermost of the three, inside CORS for the same reason as BodyLimit: a
# 500 that names its request id is no use if the browser cannot read it.
app.add_middleware(observability.ServerErrors)
# Added before CORS so CORS wraps it: a 413 still carries the CORS headers,
# and the browser shows the reason instead of an opaque network error.
app.add_middleware(
    BodyLimit,
    default=get_settings().max_body_bytes,
    transcript=get_settings().max_transcript_body_bytes,
)
_configure_cors(app)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Name the field and the rule it broke, never the value that broke it.

    Pydantic records the rejected value in `input`, and FastAPI's default
    handler passes it straight back — so a password one character over the
    limit, or an API key pasted into the settings form, is echoed in the
    response body and into whatever logs or error reporters see it. The
    caller submitted the value and does not need it read back; the field and
    the reason are the whole useful part.
    """
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": jsonable_encoder(
                [
                    {"type": e.get("type", ""), "loc": e.get("loc", ()), "msg": e.get("msg", "")}
                    for e in exc.errors()
                ]
            )
        },
    )


@app.middleware("http")
async def audit(request: Request, call_next):
    started = time.perf_counter()
    rid = observability.new_request_id(request.headers.get(observability.REQUEST_ID_HEADER))
    token = request_id.set(rid)
    try:
        response = await call_next(request)
        response.headers[observability.REQUEST_ID_HEADER] = rid
        elapsed_ms = (time.perf_counter() - started) * 1000
        ip = client_ip(request)
        if request.url.path not in ("/health", "/"):
            logger.info(
                "%s %s -> %d (%.0fms) ip=%s",
                request.method, request.url.path, response.status_code, elapsed_ms, ip,
            )
        if observability.audited(request.method, request.url.path):
            # After the response exists, so the row has the status it got.
            # Off the event loop: it is a database write.
            await asyncio.to_thread(
                observability.record_audit,
                user=observability.actor_of(request.headers.get("authorization")),
                ip=ip,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
            )
        return response
    finally:
        request_id.reset(token)


app.include_router(routes_auth.router)
app.include_router(routes_projects.router)
app.include_router(routes_jobs.router)
app.include_router(routes_admin.router)
app.include_router(routes_usage.router)


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
    # Before uvicorn starts, so its own lines ("Started server process",
    # "Exception in ASGI application" tracebacks) go through the same stdout
    # handler as the app's. With uvicorn's default config they went to
    # stderr, which the platform files as severity "error" — every startup
    # read as a failure.
    setup_logging()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        log_config=None,
        # The `audit` middleware already writes one line per request, with
        # the client address read the safe way (security.client_ip). uvicorn's
        # access line would be a second one that includes the query string
        # and the first, client-chosen X-Forwarded-For hop.
        access_log=False,
        # Railway terminates TLS and forwards X-Forwarded-*; without this
        # uvicorn reports every client as the proxy's own address.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
