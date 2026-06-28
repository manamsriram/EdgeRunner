"""Shared FastAPI dependencies — singletons for config, repo, broker, and auth."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import HTTPException, Request

load_dotenv()

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY == "change-me-in-production-please":
    raise RuntimeError(
        "JWT_SECRET_KEY is not set or is the default placeholder. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_HOURS = 8

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


# ---- JWT helpers ----


def _make_token(subject: str, expires_delta: timedelta) -> str:
    payload = {
        "sub": subject,
        "exp": datetime.now(timezone.utc) + expires_delta,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def make_access_token(username: str) -> str:
    return _make_token(username, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))


def make_refresh_token(username: str) -> str:
    return _make_token(username, timedelta(hours=REFRESH_TOKEN_EXPIRE_HOURS))


def decode_token(token: str) -> str:
    """Decode JWT and return username; raises HTTPException on any failure."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")


def get_current_user(request: Request) -> str:
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    return decode_token(token)


# ---- auth DB (Postgres / Supabase) ----

_pg_auth_schema_initialized = False
_PG_AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username   TEXT PRIMARY KEY,
    password   TEXT NOT NULL,
    email      TEXT UNIQUE,
    full_name  TEXT,
    created_at TEXT
);
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


def _ensure_pg_auth_schema() -> None:
    global _pg_auth_schema_initialized
    if _pg_auth_schema_initialized:
        return
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_PG_AUTH_SCHEMA)
    _pg_auth_schema_initialized = True


# ---- password helpers ----


def verify_and_upgrade(plain: str, username: str) -> bool:
    """Verify password against bcrypt hash stored in Postgres."""
    _ensure_pg_auth_schema()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT password FROM users WHERE username=%s", (username,))
            row = cur.fetchone()
    if not row:
        return False
    stored: str = row["password"]
    if stored.startswith("$2"):
        return bcrypt.checkpw(plain.encode(), stored.encode())
    return False


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def get_user(username: str) -> dict | None:
    _ensure_pg_auth_schema()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, email, full_name FROM users WHERE username=%s", (username,)
            )
            row = cur.fetchone()
    return dict(row) if row else None


def username_exists(username: str) -> bool:
    _ensure_pg_auth_schema()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
            return cur.fetchone() is not None


def email_exists(email: str) -> bool:
    _ensure_pg_auth_schema()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE email=%s", (email,))
            return cur.fetchone() is not None


def create_user(username: str, email: str, full_name: str, plain_password: str) -> None:
    _ensure_pg_auth_schema()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password, email, full_name, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (username, hash_password(plain_password), email, full_name,
                 datetime.now(timezone.utc).isoformat()),
            )


def save_query(username: str, query: str, response: str) -> None:
    _ensure_pg_auth_schema()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO queries (username, query, response, timestamp) "
                "VALUES (%s, %s, %s, %s)",
                (username, query, response, datetime.now(timezone.utc).isoformat()),
            )


def get_user_history(username: str) -> list[dict]:
    _ensure_pg_auth_schema()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT query, response, timestamp FROM queries "
                "WHERE username=%s ORDER BY timestamp DESC",
                (username,),
            )
            return [dict(r) for r in cur.fetchall()]
