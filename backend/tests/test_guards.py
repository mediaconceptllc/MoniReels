"""The API's guards against configuration and traffic it must not accept.

None of these need the database: they are about what happens before a route
runs — at startup, in the middleware, and in the token check.
"""
from __future__ import annotations

import time

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from app import ratelimit
from app.bodylimit import BodyLimit
from app.config import Settings, get_settings


def _settings(**overrides) -> Settings:
    base = {"jwt_secret": "x" * 48, "cors_origins": "https://app.example"}
    return Settings(_env_file=None, **{**base, **overrides})


# ---------------------------------------------------------------------------
# Startup: a weak secret or a wildcard origin never serves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret", ["", "Qz9-tiny-secret", "Qz" * 15 + "Q"])
def test_a_short_jwt_secret_is_refused_without_echoing_it(secret):
    from app.main import startup_problems

    problems = startup_problems(_settings(jwt_secret=secret))
    assert len(problems) == 1 and "JWT_SECRET" in problems[0]
    if secret:
        assert secret not in problems[0]


def test_a_32_byte_secret_and_exact_origins_start():
    from app.main import startup_problems

    assert startup_problems(_settings(jwt_secret="x" * 32)) == []


def test_a_wildcard_origin_is_refused():
    from app.main import startup_problems

    problems = startup_problems(_settings(cors_origins="https://app.example,*"))
    assert len(problems) == 1 and "CORS_ORIGINS" in problems[0]


def test_the_api_refuses_to_start_with_a_short_secret(monkeypatch):
    """Through the real lifespan, not only the helper: a check that is never
    called protects nothing."""
    from app.main import app

    monkeypatch.setenv("JWT_SECRET", "too-short")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            with TestClient(app):
                pass
    finally:
        get_settings.cache_clear()


def test_a_wildcard_never_reaches_the_cors_middleware(monkeypatch):
    """Starlette answers allow_origins=["*"] with credentials by echoing the
    caller's origin — any site, not none."""
    from app.main import _configure_cors

    monkeypatch.setenv("CORS_ORIGINS", "*,https://app.example")
    get_settings.cache_clear()
    try:
        application = FastAPI()
        application.get("/ping")(lambda: {"ok": True})
        _configure_cors(application)
        client = TestClient(application)
        preflight = {"Access-Control-Request-Method": "GET"}
        evil = client.options("/ping", headers={"Origin": "https://evil.example", **preflight})
        good = client.options("/ping", headers={"Origin": "https://app.example", **preflight})
    finally:
        get_settings.cache_clear()
    assert evil.headers.get("access-control-allow-origin") is None
    assert good.headers.get("access-control-allow-origin") == "https://app.example"


# ---------------------------------------------------------------------------
# Request bodies have a ceiling, and it holds before anything reads them
# ---------------------------------------------------------------------------


class _Entered:
    """Sits between the limit and the app: records that the app was called
    at all, which is what "refused unread" means."""

    def __init__(self, app, log: list[str]) -> None:
        self.app, self.log = app, log

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.log.append(scope["path"])
        await self.app(scope, receive, send)


def _limited_app(reached: list[int], entered: list[str] | None = None) -> FastAPI:
    application = FastAPI()

    @application.post("/echo")
    async def echo(request: Request) -> dict:
        body = await request.body()
        reached.append(len(body))
        return {"n": len(body)}

    @application.put("/projects/p/transcript")
    async def transcript(request: Request) -> dict:
        body = await request.body()
        reached.append(len(body))
        return {"n": len(body)}

    application.add_middleware(_Entered, log=entered if entered is not None else [])
    application.add_middleware(BodyLimit, default=100, transcript=1000)
    return application


def test_a_declared_length_over_the_limit_is_refused_unread():
    reached: list[int] = []
    entered: list[str] = []
    client = TestClient(_limited_app(reached, entered))
    response = client.post("/echo", content=b"x" * 101)
    assert response.status_code == 413
    assert response.json()["detail"] == "Хүсэлт хэт том байна."
    assert entered == []  # refused on the header alone: nothing behind it ran


def test_a_chunked_body_is_counted_as_it_streams():
    """No Content-Length: the lie gains nothing, the count stops it."""
    reached: list[int] = []
    client = TestClient(_limited_app(reached))
    response = client.post("/echo", content=iter([b"x" * 60, b"x" * 60]))
    assert response.status_code == 413
    assert reached == []


def test_bodies_within_the_limit_pass_and_the_transcript_gets_more_room():
    reached: list[int] = []
    client = TestClient(_limited_app(reached))
    assert client.post("/echo", content=b"x" * 100).status_code == 200
    assert client.put("/projects/p/transcript", content=b"x" * 900).status_code == 200
    assert client.post("/echo", content=b"x" * 900).status_code == 413
    assert reached == [100, 900]


def test_an_anonymous_oversized_post_is_refused_before_the_token_check():
    """The finding itself: 30 MB to POST /projects with no token used to be
    read whole and THEN answered 401."""
    from app.main import app

    response = TestClient(app).post(
        "/projects", content=b"{" + b" " * (2 * 1024 * 1024) + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# Tokens and addresses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["exp", "iat", "sub"])
def test_a_token_missing_a_required_claim_is_refused(missing):
    """A token without `exp` would never expire; this API never mints one
    without all three, so one that lacks them was not minted here."""
    from app.security import AuthError, decode_token

    now = int(time.time())
    claims = {"sub": "someone", "iat": now, "exp": now + 60}
    del claims[missing]
    token = jwt.encode(claims, get_settings().jwt_secret, algorithm="HS256")
    with pytest.raises(AuthError):
        decode_token(token)


def test_the_client_address_reads_every_forwarded_line():
    """A client's own X-Forwarded-For line ahead of the proxy's must not be
    the one that is read."""
    from app.security import client_ip

    request = StarletteRequest(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-for", b"203.0.113.9")],
            "client": ("10.0.0.1", 1234),
        }
    )
    assert client_ip(request) == "203.0.113.9"


# ---------------------------------------------------------------------------
# The rate limiter forgets callers once their window has passed
# ---------------------------------------------------------------------------


def test_expired_keys_are_swept(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(ratelimit.time, "time", lambda: clock[0])
    for n in range(500):
        ratelimit.login_allowed(f"user{n}", f"10.0.{n // 250}.{n % 250}")
    assert ratelimit.tracked_keys() > 500

    clock[0] += ratelimit.USER_WINDOW_SEC + ratelimit.SWEEP_EVERY_SEC + 1
    ratelimit.login_allowed("late", "10.9.9.9")
    assert ratelimit.tracked_keys() == 2  # only the caller that just arrived


def test_a_password_change_is_bounded_per_account():
    allowed = [ratelimit.password_change_allowed("acct") for _ in range(ratelimit.PASSWORD_MAX_ATTEMPTS + 1)]
    assert allowed[:-1] == [True] * ratelimit.PASSWORD_MAX_ATTEMPTS and allowed[-1] is False
    # Another account is untouched, and success clears the count.
    assert ratelimit.password_change_allowed("other")
    ratelimit.password_change_succeeded("acct")
    assert ratelimit.password_change_allowed("acct")
