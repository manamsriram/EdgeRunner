"""Session cookie auth: wrong/missing/expired/valid tokens all resolve correctly."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api import deps


def _request(cookie: str | None) -> Request:
    headers = [(b"cookie", f"{deps.COOKIE_NAME}={cookie}".encode())] if cookie else []
    scope = {"type": "http", "headers": headers}
    return Request(scope)


def _token(secret: str, *, expired: bool = False) -> str:
    now = datetime.now(timezone.utc)
    exp = now - timedelta(days=1) if expired else now + timedelta(days=1)
    return jwt.encode({"sub": "admin", "iat": now, "exp": exp}, secret, algorithm="HS256")


def test_no_secret_configured_is_open(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: type("C", (), {"auth_secret": None})())
    assert deps.get_current_user(_request(None)) == "admin"


def test_missing_cookie_rejected(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: type("C", (), {"auth_secret": "s3cr3t"})())
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(_request(None))
    assert exc.value.status_code == 401


def test_valid_cookie_accepted(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: type("C", (), {"auth_secret": "s3cr3t"})())
    token = _token("s3cr3t")
    assert deps.get_current_user(_request(token)) == "admin"


def test_expired_cookie_rejected(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: type("C", (), {"auth_secret": "s3cr3t"})())
    token = _token("s3cr3t", expired=True)
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(_request(token))
    assert exc.value.status_code == 401


def test_wrong_secret_rejected(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: type("C", (), {"auth_secret": "s3cr3t"})())
    token = _token("wrong-secret")
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(_request(token))
    assert exc.value.status_code == 401
