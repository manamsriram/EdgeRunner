"""Single-user session auth: a long-lived JWT bearer token, no login form.

Bootstrap once per browser: POST the `AUTH_SECRET` value as a JSON body to
`/api/auth/session` (e.g. via curl or the browser console — never as a URL
query param, which would land in server access logs, browser history, and
Referer headers). The response body carries the token; the frontend stores
it in localStorage and sends it as `Authorization: Bearer <token>` on every
request — a cookie doesn't work here since the frontend (Vercel) and backend
(Render) are different domains, and browsers increasingly block or partition
third-party cookies regardless of SameSite/Secure settings.

No password, no username — the secret you POST *is* the credential, so
treat it like one (long random value, HTTPS only, never committed).
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.deps import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_SESSION_DAYS = 60


class _SessionRequest(BaseModel):
    key: str


@router.post("/session")
def create_session(body: _SessionRequest):
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
    logger.info("session token issued")
    return {"ok": True, "token": token}
