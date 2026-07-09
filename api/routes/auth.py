"""Single-user session auth: a long-lived JWT cookie, no login form.

Bootstrap once per browser: POST the `AUTH_SECRET` value as a JSON body to
`/api/auth/session` (e.g. via curl — never as a URL query param, which would
land in server access logs, browser history, and Referer headers). That sets
an HttpOnly cookie signed with the same secret; every other route verifies
it via `get_current_user`. No password, no username — the secret you POST
*is* the credential, so treat it like one (long random value, HTTPS only,
never committed).
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from api.deps import COOKIE_NAME, get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_SESSION_DAYS = 60


class _SessionRequest(BaseModel):
    key: str


@router.post("/session")
def create_session(body: _SessionRequest, response: Response):
    secret = get_config().auth_secret
    if not secret:
        raise HTTPException(status_code=503, detail="AUTH_SECRET not configured on server")
    if not hmac.compare_digest(body.key, secret):
        raise HTTPException(status_code=401, detail="invalid key")

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {"sub": "admin", "iat": now, "exp": now + timedelta(days=_SESSION_DAYS)},
        secret,
        algorithm="HS256",
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=_SESSION_DAYS * 24 * 3600,
        path="/",
    )
    logger.info("session cookie issued")
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
