"""A ceiling on request bodies, enforced before anything reads one.

FastAPI parses a JSON body before the route's dependencies run — so before
the token is checked. With no ceiling, an anonymous client could make the
single API process read 30 MB per request (measured: heap 122 MB for one),
and a handful at once would take the API away from every signed-in user.

Two ways a body arrives, and both are bounded:

* a `Content-Length` larger than the limit is refused outright, unread;
* a chunked body (no length) is counted as it streams and refused the moment
  it crosses the limit, so a lie about the length gains nothing either.

Raw ASGI rather than `@app.middleware("http")`: it has to see the body's
chunks as they arrive, and a request-level middleware never does.
"""
from __future__ import annotations

import json

from fastapi import HTTPException, status

TOO_LARGE = "Хүсэлт хэт том байна."


class BodyLimit:
    def __init__(self, app, *, default: int, transcript: int) -> None:
        self.app = app
        self.default = default
        self.transcript = transcript

    def limit_for(self, method: str, path: str) -> int:
        # The one route that carries many lines of text: a corrected
        # transcript (only the changed lines, but a long video has thousands).
        if method == "PUT" and path.startswith("/projects/") and path.endswith("/transcript"):
            return self.transcript
        return self.default

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope["method"], scope["path"])

        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                too_large = int(declared) > limit
            except ValueError:
                too_large = False  # malformed: the server's own parser refuses it
            if too_large:
                await _refuse(send)
                return

        received = 0

        async def counted():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Raised inside the route's read, where the app's own
                    # exception handling turns it into a 413 like any other.
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=TOO_LARGE
                    )
            return message

        await self.app(scope, counted, send)


async def _refuse(send) -> None:
    body = json.dumps({"detail": TOO_LARGE}, ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status.HTTP_413_CONTENT_TOO_LARGE,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # The client is still sending: do not leave the connection
                # half-read for the next request to trip over.
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
