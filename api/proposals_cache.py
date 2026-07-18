"""In-process TTL cache around PostgresRepository.list_pending_proposals().

Design summary (DO NOT strip):
Read-through with eager invalidate and a version-counter signal — NOT pure
read-through. The TTL window coalesces poller reads, but post-write UX relies
on every approve/reject path calling invalidate() immediately; removing them
re-introduces up-to-TTL latency AND breaks the broker-rollback broadcast
because the version-counter signal disappears. Don't strip the invalidate
call sites without also reworking the WebSocket poller's reseed logic — the
two are coupled.

Shared between:
- api.ws.proposal_poller (WS broadcast source — was hitting DB every 3 s)
- api.routes.proposals.list_proposals (dashboard initial-load endpoint)

Without this, every 3 s the WS poller made a fresh psycopg2.connect() and a
SELECT regardless of whether anyone was connected or whether anything changed —
the largest single contributor to Supabase egress on this stack.

A 10 s TTL caps WS broadcast latency for newly-created proposals at ~13 s
(TTL + 3 s poll cadence) and caps the staleness window for any approve/reject
that misses invalidate(). Both are still well below the "click Refresh"
latency the pre-WebSocket UI exposed, so the trade-off is fine.

Scope notes:
- The in-process _scheduler_loop and _crypto_scheduler_loop in api/main.py
  share this same Python process and call repo.create_proposal(); we don't
  reach into trader.pipeline to invalidate because that would create an
  api ← trader cross-layer dependency. The TTL is the safety net.
- In-memory cache assumes a single FastAPI worker. api/main.py:148 already
  refuses to start the schedulers when WEB_CONCURRENCY>1, so the rest of the
  app is implicitly single-worker too. If that ever changes (>1 worker),
  each worker has its own cache and a recently-approved proposal could
  appear "pending" on another worker for up to TTL seconds. The atomic
  UPDATE in try_approve_proposal still prevents double-execution; the
  dashboard just looks briefly stale.
"""
from __future__ import annotations

import threading
import time

_TTL = 10.0  # seconds; tunable via monkeypatch in tests
# Single-writer/single-reader lock that serializes invalidate(), get_pending(),
# and get_pending_with_version() against each other. Without it, a get_pending()
# running concurrently with invalidate() can pair a stale "result" with the
# post-invalidate "version" stamp — which would tell consumers their data is
# current when in truth it predates the most recent row write.
_cache_lock = threading.Lock()
# `version` increments on every invalidate() call. Consumers (e.g. proposal_poller)
# track the last version they saw; when it bumps, they know the cached data may
# have changed in ways that the simple `seen_ids` diff cannot represent (most
# notably: a proposal whose status has rolled back to PENDING).
_cache: dict = {"result": None, "computed_at": 0.0, "version": 0}


def get_pending(repo) -> list[dict]:
    """Return the cached pending-proposals list, refreshing from DB if stale.

    The first call within a `_TTL` window pays for exactly one DB query;
    subsequent calls in the same window share that result. Approve and
    reject explicitly invalidate() the cache after their state transition
    commits, so the next read is fresh even well before TTL expiry.
    """
    return get_pending_with_version(repo)[0]


def get_pending_with_version(repo) -> tuple[list[dict], int]:
    """Atomic (result, version_at_fetch) reader.

    `invalidate()` is held under the same lock as this function, so the result
    and version returned here always agree: either both reflect state from
    before the most recent invalidate, or both reflect state from after it.
    No TOCTOU window where stale data could be paired with a fresh version.

    Pre-fetch version is captured before the DB call so that any
    invalidate() that lands mid-query bumps the cache's version but is
    still reported as a version diff to the consumer (the consumer will
    see the post-invalidate DB state on its NEXT call, bounded by the
    polling cadence).
    """
    with _cache_lock:
        now = time.monotonic()
        cached_result = _cache["result"]
        if cached_result is not None and (now - _cache["computed_at"]) < _TTL:
            return cached_result, _cache["version"]
        version_before_fetch = _cache["version"]
        result = repo.list_pending_proposals()
        _cache["result"] = result
        _cache["computed_at"] = time.monotonic()
        return result, version_before_fetch


def invalidate() -> None:
    """Drop the cached pending list so the next get_pending() refetches.

    Call from any code-path that mutates proposal status (try_approve_proposal,
    set_proposal_status to EXECUTED, the rollback-to-PENDING branch, reject).
    Missed invalidation paths are bounded by `_TTL`.
    """
    with _cache_lock:
        _cache["computed_at"] = 0.0
        _cache["version"] += 1


def current_version() -> int:
    """Snapshot the cache's version counter under the cache lock. Used by the
    poller to seed its last-seen-version on startup."""
    with _cache_lock:
        return _cache["version"]


def _reset_for_tests() -> None:
    """Test helper. Drops the cache (result, computed_at, version) under the
    cache lock so each test starts from a clean slate."""
    with _cache_lock:
        _cache["result"] = None
        _cache["computed_at"] = 0.0
        _cache["version"] = 0
