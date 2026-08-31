"""Sign in, identity, password change."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ratelimit
from app.config import get_settings
from app.db import get_db
from app.dbmodels import User
from app.schemas import ChangePasswordIn, LoginIn, MeOut, TokenOut
from app.security import (
    Principal,
    client_ip,
    current_user,
    hash_password,
    make_token,
    stamp_password_change,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:  # noqa: B008
    ip = client_ip(request)
    if not ratelimit.login_allowed(body.username, ip):
        # 429, not 401: the credentials were never checked, and saying so
        # keeps this from reading as "wrong password" to a legitimate user.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Нэвтрэх оролдлого хэт олон боллоо. Хэдэн минутын дараа дахин оролдоно уу.",
        )

    user = db.scalar(select(User).where(User.username == body.username))
    # The password is verified even when the user does not exist, against a
    # throwaway salt: skipping it returns visibly faster for unknown
    # usernames, which is a free account-enumeration oracle.
    if user is None:
        hash_password(body.password, "00" * 16)
        raise _bad_credentials()
    if not user.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Энэ бүртгэл хаагдсан байна.")
    if not verify_password(body.password, user.pw_salt, user.pw_hash):
        raise _bad_credentials()

    ratelimit.login_succeeded(body.username, ip)
    settings = get_settings()
    return TokenOut(
        token=make_token(user),
        username=user.username,
        role=user.role,
        expires_in_s=settings.jwt_hours * 3600,
    )


@router.get("/me", response_model=MeOut)
def me(principal: Principal = Depends(current_user)) -> MeOut:  # noqa: B008
    return MeOut(id=principal.id, username=principal.username, role=principal.role)


@router.post("/password", response_model=TokenOut)
def change_password(
    body: ChangePasswordIn,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> TokenOut:
    user = db.get(User, principal.id)
    if user is None or not verify_password(body.current_password, user.pw_salt, user.pw_hash):
        raise _bad_credentials("Current password is incorrect")

    salt, digest = hash_password(body.new_password)
    user.pw_salt, user.pw_hash = salt, digest
    # Every token issued before now stops working. That is the entire point
    # of changing a password after a suspected compromise — without this the
    # attacker's session simply continues until it expires.
    stamp_password_change(user)
    db.commit()

    # The user changing their own password must not be signed out by it, so
    # a fresh token is issued for the client to swap in.
    db.refresh(user)
    return TokenOut(
        token=make_token(user),
        username=user.username,
        role=user.role,
        expires_in_s=get_settings().jwt_hours * 3600,
    )


def _bad_credentials(detail: str = "Incorrect username or password") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
