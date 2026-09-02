"""Reading an error body from an outside service.

One definition, because three clients need it and the rule they have to obey
is the same for all three: a provider's error body is NOT ours to hand on.

It reaches a user. A failing job records its exception text in `jobs.error`,
which `GET /jobs/{id}` returns to the project's owner — so whatever the
provider chose to put in that body is what the person sees. MEASURED: an
OpenRouter 400 arrived carrying an account identifier beside its message:

    {"error": {"message": "… is not a valid model ID", "code": 400},
     "user_id": "user_2oISXvis…"}

Not a secret, and the route is behind a login. But it is the only path by
which text nobody here controls reaches a user, and the useful half of it is
always the same field. So the message is taken and the rest is left where it
came from — the operator still has the whole body in the logs.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

#: Long enough for a provider's real sentence, short enough that a body which
#: is prose rather than JSON cannot fill a database column.
MESSAGE_MAX = 300


@dataclass(frozen=True)
class ProviderError:
    """What could be read out of the body. Both fields may be absent — a
    bodiless 520 from a CDN in front of the provider is a real case here."""

    code: str | None = None
    message: str | None = None


def read_error(response: httpx.Response, limit: int = MESSAGE_MAX) -> ProviderError:
    """The provider's own `code` and `message`, and nothing else.

    Two shapes are accepted because providers disagree: `{"error": {...}}`
    and a flat `{"code": ..., "message": ...}`. A body that is neither — HTML
    from a proxy, an empty 520 — yields an empty result rather than a
    fallback to the raw text, which is the whole point.
    """
    try:
        body = response.json()
    except ValueError:
        return ProviderError()
    if not isinstance(body, dict):
        return ProviderError()

    error = body.get("error")
    if isinstance(error, str):
        # Some gateways answer `{"error": "rate limited"}`.
        return ProviderError(message=error.strip()[:limit] or None)
    if not isinstance(error, dict):
        error = body

    code = error.get("code")
    message = error.get("message") or error.get("detail")
    return ProviderError(
        # A numeric code (OpenRouter sends the status again) is still a code.
        code=str(code) if code is not None else None,
        message=str(message).strip()[:limit] or None if message is not None else None,
    )
