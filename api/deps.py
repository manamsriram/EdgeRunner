"""Shared FastAPI dependencies — singletons for config, repo, broker, and auth."""
from __future__ import annotations

import hashlib
import os
import sqlite3
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
    if cfg.database_url:
        from trader.portfolio.postgres_repo import PostgresRepository
        return PostgresRepository(cfg.database_url)
    from trader.portfolio.sqlite_repo import SQLiteRepository
    return SQLiteRepository(cfg.portfolio_db_path)


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


# ---- auth DB: SQLite path ----

def _db_path() -> str:
    return get_config().portfolio_db_path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ---- auth DB: Postgres path ----

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


# ---- password helpers (backend-agnostic) ----


def verify_and_upgrade(plain: str, username: str) -> bool:
    """Verify password; silently upgrade SHA256 → bcrypt on first successful login."""
    if get_config().database_url:
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
        if hashlib.sha256(plain.encode()).hexdigest() != stored:
            return False
        new_hash = bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET password=%s WHERE username=%s", (new_hash, username))
        return True

    with _connect() as conn:
        row = conn.execute(
            "SELECT password FROM users WHERE username=?", (username,)
        ).fetchone()
    if not row:
        return False
    stored = row["password"]
    if stored.startswith("$2"):
        return bcrypt.checkpw(plain.encode(), stored.encode())
    if hashlib.sha256(plain.encode()).hexdigest() != stored:
        return False
    new_hash = bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute("UPDATE users SET password=? WHERE username=?", (new_hash, username))
        conn.commit()
    return True


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def get_user(username: str) -> dict | None:
    if get_config().database_url:
        _ensure_pg_auth_schema()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT username, email, full_name FROM users WHERE username=%s", (username,)
                )
                row = cur.fetchone()
        return dict(row) if row else None

    with _connect() as conn:
        row = conn.execute(
            "SELECT username, email, full_name FROM users WHERE username=?", (username,)
        ).fetchone()
    return dict(row) if row else None


def username_exists(username: str) -> bool:
    if get_config().database_url:
        _ensure_pg_auth_schema()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
                return cur.fetchone() is not None

    with _connect() as conn:
        return bool(
            conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        )


def email_exists(email: str) -> bool:
    if get_config().database_url:
        _ensure_pg_auth_schema()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM users WHERE email=%s", (email,))
                return cur.fetchone() is not None

    with _connect() as conn:
        return bool(
            conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
        )


def create_user(username: str, email: str, full_name: str, plain_password: str) -> None:
    if get_config().database_url:
        _ensure_pg_auth_schema()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (username, password, email, full_name, created_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (username, hash_password(plain_password), email, full_name,
                     datetime.now(timezone.utc).isoformat()),
                )
        return

    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password, email, full_name, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, hash_password(plain_password), email, full_name,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def save_query(username: str, query: str, response: str) -> None:
    if get_config().database_url:
        _ensure_pg_auth_schema()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO queries (username, query, response, timestamp) "
                    "VALUES (%s, %s, %s, %s)",
                    (username, query, response, datetime.now(timezone.utc).isoformat()),
                )
        return

    with _connect() as conn:
        conn.execute(
            "INSERT INTO queries (username, query, response, timestamp) VALUES (?, ?, ?, ?)",
            (username, query, response, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_user_history(username: str) -> list[dict]:
    if get_config().database_url:
        _ensure_pg_auth_schema()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT query, response, timestamp FROM queries "
                    "WHERE username=%s ORDER BY timestamp DESC",
                    (username,),
                )
                return [dict(r) for r in cur.fetchall()]

    with _connect() as conn:
        rows = conn.execute(
            "SELECT query, response, timestamp FROM queries "
            "WHERE username=? ORDER BY timestamp DESC",
            (username,),
        ).fetchall()
    return [dict(r) for r in rows]
