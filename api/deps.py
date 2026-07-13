"""Shared FastAPI dependencies — singletons for config, repo, and broker."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache

import jwt
from dotenv import load_dotenv
from fastapi import HTTPException, Request

load_dotenv()

logger = logging.getLogger(__name__)

# Per-request auth-failure logs sit at DEBUG so a normal deploy (LOG_LEVEL=INFO) stays
# quiet. That would make a 401 burst (credential-stuffing, broken client) invisible, so
# keep a process-global counter and emit a rate-limited WARNING summary — countable
# above DEBUG without a per-request log line or a metrics backend.
# ponytail: in-process counter; swap for a real metric if Prometheus/StatsD lands.
_auth_failures = 0
_auth_failures_lock = threading.Lock()
_last_auth_warn = 0.0
_AUTH_WARN_INTERVAL = 60.0


def _record_auth_failure() -> None:
    global _auth_failures, _last_auth_warn
    with _auth_failures_lock:
        _auth_failures += 1
        now = time.monotonic()
        if now - _last_auth_warn >= _AUTH_WARN_INTERVAL:
            logger.warning("auth failures (401) total=%d since process start", _auth_failures)
            _last_auth_warn = now


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


_unauth_warned = False  # module-level: warn about open API only once, not per request


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
        # WARNING, not debug: this grants admin to every request. On a deployed box
        # (missing SUPABASE_URL is the documented Render footgun) this line is the only
        # signal the whole API is open — it must survive the default INFO log level.
        # Log once, not per request, so the signal isn't drowned in its own spam.
        global _unauth_warned
        if not _unauth_warned:
            logger.warning("no SUPABASE_URL or SUPABASE_JWT_SECRET set — API is unauthenticated")
            _unauth_warned = True
        request.state.auth_sub = "admin"
        return "admin"

    auth_header = request.headers.get("authorization") or ""
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        logger.debug(
            "no bearer token on request (path=%s, auth_header_present=%s)",
            request.scope.get("path", "?"), bool(auth_header),
        )
        _record_auth_failure()
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
        _record_auth_failure()
        raise HTTPException(status_code=401, detail="invalid or expired session")
    # Expose the stable subject id for audit logging without leaking the email (PII).
    request.state.auth_sub = payload["sub"]
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
