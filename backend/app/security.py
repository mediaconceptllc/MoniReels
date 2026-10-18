"""Authentication and authorization.

Every access decision is made HERE, on the server. The web client hides UI
it believes the user cannot use, but that is a convenience, not a control —
an endpoint without a `Depends(require_...)` is an open endpoint no matter
what the frontend renders.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.dbmodels import User

# Deliberately expensive. Each verification burns ~100ms of CPU, which is the
# whole point against offline cracking — and exactly why the login route must
# be rate limited (app.ratelimit): unbounded, a few dozen concurrent attempts
# stall the entire API.
PBKDF2_ROUNDS = 200_000

ROLES = ("admin", "editor")

_bearer = HTTPBearer(auto_error=False)


class AuthError(HTTPException):
    def __init__(self, detail: str = "Нэвтрэх шаардлагатай.") -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


@dataclass(frozen=True)
class Principal:
    id: str
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return salt, digest.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, actual = hash_password(password, salt)
    # Constant-time: a plain `==` leaks how many leading characters matched.
    return hmac.compare_digest(actual, expected_hash)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def stamp_password_change(user: User) -> None:
    """Invalidate every token issued under the current serial.

    Uses a counter, not a timestamp. A JWT's `iat` has one-second resolution,
    so a time comparison leaves a window: a token issued in the same second
    as the change compares equal and survives — and, worse, the replacement
    token handed back to the user looks OLDER than the change that produced
    it, signing them out immediately. Both were observed before the serial
    existed.

    There are two paths that change a password — self-service and an admin
    reset — so the rule lives here rather than at each call site. Repeat it by
    hand and one path eventually forgets, leaving exactly the sessions it was
    meant to end alive.
    """
    user.pw_changed_at = time.time()
    user.token_serial = (user.token_serial or 0) + 1


def make_token(user: User) -> str:
    settings = get_settings()
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET is not set")
    now = int(time.time())
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "ser": user.token_serial or 0,
        "iat": now,
        "exp": now + settings.jwt_hours * 3600,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        # Every token this API mints carries all three; one without them was
        # not minted here, and a missing `exp` would otherwise never expire.
        return jwt.decode(
            token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["exp", "iat", "sub"]}
        )
    except jwt.ExpiredSignatureError as e:
        raise AuthError("Нэвтрэх хугацаа дууссан байна. Дахин нэвтэрнэ үү.") from e
    except jwt.InvalidTokenError as e:
        raise AuthError("Нэвтрэлт хүчингүй байна. Дахин нэвтэрнэ үү.") from e


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> Principal:
    if credentials is None:
        raise AuthError()
    payload = decode_token(credentials.credentials)

    user = db.get(User, payload.get("sub", ""))
    if user is None or not user.active:
        raise AuthError("Энэ бүртгэл хаагдсан байна.")

    # A password change invalidates every token issued under the previous
    # serial. Checking only `exp` would leave a stolen token working for
    # hours after the owner reacted to the theft, which makes "change your
    # password immediately" advice meaningless.
    #
    # A token minted before this column existed carries no `ser`; it reads as
    # 0, which matches an account that has never changed its password. That
    # is deliberate — a migration must not sign out everyone who is already
    # logged in.
    if payload.get("ser", 0) != (user.token_serial or 0):
        raise AuthError("Нууц үг солигдсон тул нэвтрэлт дууссан. Дахин нэвтэрнэ үү.")

    return Principal(id=user.id, username=user.username, role=user.role)


def require_role(*roles: str):
    """Dependency factory. `admin` satisfies every requirement."""

    def _check(principal: Principal = Depends(current_user)) -> Principal:  # noqa: B008
        if principal.role == "admin" or principal.role in roles:
            return principal
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    return _check


require_admin = require_role("admin")
require_editor = require_role("editor")


def client_ip(request: Request) -> str:
    """The caller's address, counted from the END of X-Forwarded-For.

    A client can send the header itself and a proxy APPENDS rather than
    replaces, so the FIRST entry may be attacker-chosen. Railway puts exactly
    one proxy in front, so the last entry is the one it added.

    Every header line counts, not only the first: a client that sends its
    own X-Forwarded-For line ahead of the proxy's would otherwise have its
    line read and the proxy's ignored.
    """
    forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"
