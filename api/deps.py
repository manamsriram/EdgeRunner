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


def get_current_user(request: Request) -> str:
    """Verifies a Supabase Auth JWT sent as `Authorization: Bearer <token>`.

    Frontend signs in via supabase-js (supabase.auth.signInWithPassword); the
    resulting session's access_token is what arrives here. Verified against the
    project's JWT secret (Settings → API → JWT Settings in the Supabase dashboard) —
    Supabase signs these HS256 with `aud: "authenticated"`.

    If SUPABASE_JWT_SECRET isn't configured, auth is off (dev default) — logged once
    per process so it's never silently open in a deployed environment.
    """
    secret = get_config().supabase_jwt_secret
    if not secret:
        logger.warning("SUPABASE_JWT_SECRET not set — API is unauthenticated")
        return "admin"

    auth_header = request.headers.get("authorization") or ""
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated")
    except jwt.PyJWTError:
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
