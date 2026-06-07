"""Auth router: login, refresh, logout, me."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from api.deps import (
    decode_token,
    get_current_user,
    get_user,
    get_user_history,
    make_access_token,
    make_refresh_token,
    verify_and_upgrade,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_SECURE_COOKIES = os.getenv("SECURE_COOKIES", "false").strip().lower() in {"1", "true", "yes"}
# Cross-origin (Vercel frontend + Render API): SameSite=None;Secure required.
# Local dev (same origin): SameSite=Strict is fine and doesn't need HTTPS.
_SAMESITE = "none" if _SECURE_COOKIES else "strict"


def _set_auth_cookies(response: Response, username: str) -> None:
    kw = dict(httponly=True, samesite=_SAMESITE, secure=_SECURE_COOKIES)
    response.set_cookie("access_token", make_access_token(username), max_age=15 * 60, **kw)
    response.set_cookie("refresh_token", make_refresh_token(username), max_age=8 * 3600, **kw)


# ---- request schemas ----


class LoginRequest(BaseModel):
    username: str
    password: str


# ---- endpoints ----


@router.post("/login")
def login(body: LoginRequest, response: Response):
    if not verify_and_upgrade(body.password, body.username):
        raise HTTPException(status_code=401, detail="invalid credentials")
    _set_auth_cookies(response, body.username)
    user = get_user(body.username)
    return {"username": user["username"], "full_name": user["full_name"]}


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=401, detail="no refresh token")
    username = decode_token(token)
    _set_auth_cookies(response, username)
    return {"username": username}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return {"message": "logged out"}


@router.get("/me")
def me(username: str = Depends(get_current_user)):
    user = get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    return user


@router.get("/history")
def history(username: str = Depends(get_current_user)):
    return get_user_history(username)
