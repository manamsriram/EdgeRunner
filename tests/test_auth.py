"""Supabase Auth JWT verification: wrong/missing/expired/valid tokens all resolve correctly."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api import deps


def _cfg(**kw):
    """Config stub — defaults both auth knobs off unless overridden."""
    return type("C", (), {"supabase_jwt_secret": None, "supabase_url": None, **kw})()


def _request(bearer: str | None) -> Request:
    headers = [(b"authorization", f"Bearer {bearer}".encode())] if bearer else []
    scope = {"type": "http", "headers": headers}
    return Request(scope)


def _token(secret: str, *, expired: bool = False, aud: str = "authenticated") -> str:
    now = datetime.now(timezone.utc)
    exp = now - timedelta(days=1) if expired else now + timedelta(days=1)
    return jwt.encode(
        {"sub": "user-id-123", "email": "sriram@example.com", "aud": aud, "iat": now, "exp": exp},
        secret,
        algorithm="HS256",
    )


def test_no_secret_configured_is_open(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: _cfg())
    assert deps.get_current_user(_request(None)) == "admin"


def test_missing_header_rejected(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: _cfg(supabase_jwt_secret="s3cr3t"))
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(_request(None))
    assert exc.value.status_code == 401


def test_valid_token_accepted(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: _cfg(supabase_jwt_secret="s3cr3t"))
    token = _token("s3cr3t")
    assert deps.get_current_user(_request(token)) == "sriram@example.com"


def test_expired_token_rejected(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: _cfg(supabase_jwt_secret="s3cr3t"))
    token = _token("s3cr3t", expired=True)
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(_request(token))
    assert exc.value.status_code == 401


def test_wrong_secret_rejected(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: _cfg(supabase_jwt_secret="s3cr3t"))
    token = _token("wrong-secret")
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(_request(token))
    assert exc.value.status_code == 401


def test_wrong_audience_rejected(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: _cfg(supabase_jwt_secret="s3cr3t"))
    token = _token("s3cr3t", aud="anon")
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(_request(token))
    assert exc.value.status_code == 401


def test_malformed_token_returns_401_not_500(monkeypatch):
    """A garbage bearer value makes get_unverified_header raise; the handler must
    still end in a 401, never a 500."""
    monkeypatch.setattr(deps, "get_config", lambda: _cfg(supabase_jwt_secret="s3cr3t"))
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(_request("not-a-jwt"))
    assert exc.value.status_code == 401


def test_401_increments_auth_failure_counter(monkeypatch):
    """Per-request auth logs are DEBUG; a 401 must still bump the process counter so a
    burst is countable above DEBUG without a per-request log line."""
    monkeypatch.setattr(deps, "get_config", lambda: _cfg(supabase_jwt_secret="s3cr3t"))
    monkeypatch.setattr(deps, "_auth_failures", 0)
    monkeypatch.setattr(deps, "_last_auth_warn", 0.0)
    for _ in range(3):
        with pytest.raises(HTTPException):
            deps.get_current_user(_request("not-a-jwt"))
    assert deps._auth_failures == 3


def test_es256_jwks_token_accepted(monkeypatch):
    """Current Supabase default: access tokens signed ES256, verified via JWKS public key."""
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {"sub": "user-id-123", "email": "sriram@example.com", "aud": "authenticated",
         "iat": now, "exp": now + timedelta(days=1)},
        priv, algorithm="ES256",
    )
    monkeypatch.setattr(deps, "get_config",
                        lambda: _cfg(supabase_url="https://proj.supabase.co"))
    monkeypatch.setattr(deps, "_jwks_client",
                        lambda: type("J", (), {"get_signing_key_from_jwt":
                                               lambda self, t: type("K", (), {"key": priv.public_key()})()})())
    assert deps.get_current_user(_request(token)) == "sriram@example.com"
