"""Who did what, and which request or job a failure belongs to.

Three things the audit found missing (OPS-3), kept in one place so the API
and the worker wire them the same way:

- **Request IDs.** Every HTTP request gets one — the caller's `X-Request-ID`
  when it is a plausible id, a fresh one otherwise — and it travels in the
  response header, in every log line written while serving it, in a 500's
  body and in the error report. "It failed at about ten past" becomes one
  search.
- **Error reports.** Optional: set `SENTRY_DSN` and unhandled errors, and
  every ERROR log line, reach Sentry from both services, tagged with the
  request or job. Unset, nothing is imported and nothing leaves the
  process. The SDK is told to send no request bodies, no cookies and no
  Authorization header: a password, a provider key pasted into the settings
  form, or a bearer token must never become an event.
- **The admin audit trail.** `audit_log` existed from the first migration
  and was never written. Every state-changing admin request (and a user's
  own password change) now leaves a row: who, from where, what, and the
  status it got — never the request body.
"""

from __future__ import annotations

import os
import re
import time
import uuid

from app.utils.logging import get_logger, job_id, job_kind, request_id

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
# A caller's id is kept only when it looks like one: it goes into log lines
# and a response header, so anything else — control characters, a 10 kB
# string, a header-splitting attempt — is replaced, not echoed.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def new_request_id(offered: str | None) -> str:
    if offered and _REQUEST_ID_RE.match(offered):
        return offered
    return uuid.uuid4().hex


class ServerErrors:
    """A 500 that says which request it was.

    Starlette's own answer is the bare text "Internal Server Error", produced
    outside every middleware: no CORS headers (so the page saw a network
    error, not a 500), no request id to quote, nothing to find the traceback
    by. This sits inside CORS and answers with the id that is in the log
    line, the response header and the error report. The exception is logged
    here — which is also how it reaches Sentry — and never returned: its
    text can carry paths, SQL or a provider's reply.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked(message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except Exception:
            if started:
                # Half a response is already on the wire; nothing sane can
                # follow it. Let the server close the connection.
                raise
            from fastapi.responses import JSONResponse

            rid = request_id.get() or new_request_id(None)
            logger.exception("Unhandled error on %s %s", scope.get("method"), scope.get("path"))
            response = JSONResponse(
                status_code=500,
                content={"detail": f"Серверт алдаа гарлаа. Лавлах дугаар: {rid}"},
                headers={REQUEST_ID_HEADER: rid},
            )
            await response(scope, receive, send)


# ---------------------------------------------------------------------------
# Error reports
# ---------------------------------------------------------------------------

_SCRUBBED_HEADERS = {"authorization", "cookie", "x-api-key", "set-cookie"}


def _before_send(event: dict, hint: dict) -> dict:
    """Tag the event with the request or job it came from, and take out the
    headers that carry credentials whatever the SDK's own defaults are."""
    tags = event.setdefault("tags", {})
    for name, var in (("request_id", request_id), ("job_id", job_id), ("job_kind", job_kind)):
        value = var.get()
        if value:
            tags[name] = value
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                k: ("[Filtered]" if k.lower() in _SCRUBBED_HEADERS else v) for k, v in headers.items()
            }
        request.pop("cookies", None)
        request.pop("data", None)
    return event


def init_error_reports(service: str) -> bool:
    """Start Sentry for `service` ("api" or "worker") when SENTRY_DSN is set.

    Returns whether it started. Never raises: a missing or broken SDK must
    not stop a service from running — it is logged and the service goes on
    without reports.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.sentry_dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; error reports are off")
        return False
    try:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.environment,
            # The commit Railway built, so a report names the code it came from.
            release=os.environ.get("RAILWAY_GIT_COMMIT_SHA") or None,
            server_name=service,
            send_default_pii=False,
            max_request_body_size="never",
            traces_sample_rate=settings.sentry_traces_sample_rate,
            before_send=_before_send,
        )
        sentry_sdk.set_tag("service", service)
    except Exception:  # noqa: BLE001 - reports are optional; the service is not
        logger.exception("Could not start error reports; continuing without them")
        return False
    logger.info("Error reports on (service=%s)", service)
    return True


# ---------------------------------------------------------------------------
# The admin audit trail
# ---------------------------------------------------------------------------

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def audited(method: str, path: str) -> bool:
    """A request the audit trail records: anything that changes state under
    /admin, and a user changing their own password."""
    if method.upper() in _SAFE_METHODS:
        return False
    return path.startswith("/admin/") or path == "/auth/password"


def actor_of(authorization: str | None) -> str:
    """The username the request's bearer token names, or "" — read from the
    token without the database, which this runs too late to need. A token
    that does not verify names nobody: the row then says the request was
    anonymous, which is what it was."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return ""
    from app.security import AuthError, decode_token

    try:
        payload = decode_token(authorization[7:].strip())
    except AuthError:
        return ""
    return str(payload.get("username") or payload.get("sub") or "")[:64]


def record_audit(*, user: str, ip: str, method: str, path: str, status: int) -> None:
    """One row of the audit trail. Never raises: the action it describes has
    already happened, and a failed write must not turn it into an error."""
    from app.db import session_scope
    from app.dbmodels import AuditLog

    try:
        with session_scope() as db:
            db.add(
                AuditLog(
                    at=time.time(),
                    user=user[:64],
                    ip=ip[:64],
                    method=method[:8],
                    path=path[:300],
                    status=status,
                )
            )
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("Could not write the audit row for %s %s by %r", method, path, user)


def purge_audit(keep_days: int) -> int:
    """Drop audit rows older than `keep_days` (housekeeping). The trail is
    for "who changed this, recently" — a year of it is plenty, and an
    append-only table with no end is a disk that fills one day."""
    from app.db import session_scope
    from app.dbmodels import AuditLog

    cutoff = time.time() - keep_days * 86400
    with session_scope() as db:
        return db.execute(AuditLog.__table__.delete().where(AuditLog.at < cutoff)).rowcount or 0
