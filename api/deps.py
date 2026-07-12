"""Shared FastAPI dependencies — singletons for config, repo, and broker."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache

import jwt
from dotenv import load_dotenv
from fastapi import HTTPException, Request

load_dotenv()

logger = logging.getLogger(__name__)


# ---- singletons ----


@lru_cache(maxsize=1)
def get_config():
    from trader.config import load_config
    return load_config()


@lru_cache(maxsize=1)
def get_repo():
    cfg = get_config()
    if not cfg.database_url:
        raise RuntimeError("DATABASE_URL is required — set it to your Supabase pooler URI")
    from trader.portfolio.postgres_repo import PostgresRepository
    return PostgresRepository(cfg.database_url)


@lru_cache(maxsize=1)
def get_broker():
    from trader.execution.broker import AlpacaBroker
    return AlpacaBroker(get_config())


@lru_cache(maxsize=1)
def _jwks_client():
    """Cached JWKS client for the Supabase project — fetches ES256 public keys.

    New Supabase projects sign access tokens with asymmetric keys (ES256) served
    from the project's JWKS endpoint, not the legacy HS256 shared secret. PyJWKClient
    caches the fetched keys internally, so this is one network hit per key rotation.
    """
    url = f"{get_config().supabase_url}/auth/v1/.well-known/jwks.json"
    return jwt.PyJWKClient(url)


def auth_enabled() -> bool:
    """True when a Supabase verification path is configured (URL or legacy secret)."""
    cfg = get_config()
    return bool(cfg.supabase_url or cfg.supabase_jwt_secret)


def verify_supabase_jwt(token: str) -> dict:
    """Verify a Supabase access token, returning its claims. Raises jwt.PyJWTError.

    ES256 via the project's JWKS public keys when SUPABASE_URL is set (current
    Supabase default), else legacy HS256 shared-secret. Callers must first check
    `auth_enabled()` — this always attempts verification.
    """
    cfg = get_config()
    if cfg.supabase_url:
        key = _jwks_client().get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["ES256"], audience="authenticated")
    return jwt.decode(token, cfg.supabase_jwt_secret, algorithms=["HS256"], audience="authenticated")


def get_current_user(request: Request) -> str:
    """Verifies a Supabase Auth JWT sent as `Authorization: Bearer <token>`.

    Frontend signs in via supabase-js (supabase.auth.signInWithPassword); the
    resulting session's access_token is what arrives here, with `aud: "authenticated"`.
    Auth off (dev default) when neither SUPABASE_URL nor SUPABASE_JWT_SECRET is set —
    logged so it's never silently open in a deployed environment.
    """
    if not auth_enabled():
        logger.debug("no SUPABASE_URL or SUPABASE_JWT_SECRET set — API is unauthenticated")
        return "admin"

    auth_header = request.headers.get("authorization") or ""
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        logger.debug(
            "no bearer token on request (path=%s, auth_header_present=%s)",
            request.scope.get("path", "?"), bool(auth_header),
        )
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        payload = verify_supabase_jwt(token)
    except jwt.PyJWTError as exc:
        # A malformed token can make get_unverified_header itself raise; guard it so the
        # handler always ends in a 401, never a 500.
        try:
            alg = jwt.get_unverified_header(token).get("alg")
        except jwt.PyJWTError:
            alg = "unparseable"
        logger.debug("JWT verification failed: %s (header alg=%s)", exc, alg)
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return payload.get("email") or payload["sub"]


# ---- query history ----

_history_schema_initialized = False
_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
    id        SERIAL PRIMARY KEY,
    username  TEXT NOT NULL,
    query     TEXT NOT NULL,
    response  TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
"""


def _pg_connect():
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(get_config().database_url, cursor_factory=psycopg2.extras.RealDictCursor)


def _ensure_history_schema() -> None:
    global _history_schema_initialized
    if _history_schema_initialized:
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_HISTORY_SCHEMA)
    _history_schema_initialized = True


def save_query(username: str, query: str, response: str) -> None:
    _ensure_history_schema()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO queries (username, query, response, timestamp) "
                "VALUES (%s, %s, %s, %s)",
                (username, query, response, datetime.now(timezone.utc).isoformat()),
            )
