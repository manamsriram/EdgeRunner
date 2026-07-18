"""WebSocket broadcaster + DB-polling background task.

No IPC with the scheduler process required — we poll the proposals table (via a shared
in-process TTL cache, api.proposals_cache) and broadcast diffs to connected clients.
The poller idles when nobody is connected: broadcasting into the void is wasted Supabase
egress. 3-second latency is acceptable vs. the current "click Refresh" model; the cache
TTL can stretch it to ~13 s worst case before a new proposal is broadcast.
"""
from __future__ import annotations

import asyncio
import logging
import time

import jwt
from fastapi import WebSocket, WebSocketDisconnect

from api.deps import auth_enabled, get_repo, verify_supabase_jwt

logger = logging.getLogger(__name__)


class _ConnectionManager:
    def __init__(self) -> None:
        self._active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._active.discard(ws)

    async def broadcast(self, msg: dict) -> None:
        dead: set[WebSocket] = set()
        for ws in self._active:
            try:
                await ws.send_json(msg)
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                dead.add(ws)
        self._active -= dead


manager = _ConnectionManager()


async def proposal_poller() -> None:
    """Design summary (DO NOT strip) — mirrors api/proposals_cache.py:
    api.proposals_cache.invalidate() bumps the version counter atomically
    under `_cache_lock`; the version-bump branch here reads that counter.
    Three pieces in this function are coupled with that contract and must
    change together with invalidate() call sites:
      (a) `seen_ids.clear()` on `cur_version != last_version` — without this,
          the broker-failure rollback (PENDING → APPROVED → PENDING) drops
          silently from the WS feed.
      (b) `last_version = current_version()` at startup — without this init,
          the first tick sees cur_version != last_version, fires the version-
          bump branch, and re-broadcasts every pending proposal at every
          process restart.
      (c) the version-aware branch must run BEFORE the `was_idle` silent-seed
          path — swapping them silently mis-handles a reconnect-after-recent-
          invalidate case (either skip a needed re-broadcast or flood the WS
          with every currently-pending proposal).

    The (data, version) atomicity of `get_pending_with_version` is upheld
    only by `_cache_lock`. If a future refactor of `invalidate()` drops the
    lock, the reseed logic above silently re-opens the TOCTOU window.

    Background task: polls DB while at least one client is connected.

    Egress-savings design (vs. the previous "poll forever, every 3 s"):
    1. Skips the DB call entirely when `manager._active` is empty. Nobody's
       listening, so the result isn't being broadcast anywhere; the egress
       byte cost is pure waste. Covers overnight and any tab-closed stretches.
    2. On a 0 → 1 active transition (cold start with a connected client, or
       reconnect after all clients left), silently reseeds `seen_ids` from the
       current pending list. The dashboard fetched those proposals on initial
       page load via REST; re-broadcasting them here would be duplicate UI work.
    3. When active, polls via api.proposals_cache.get_pending(repo) so a single
       DB hit per 10 s TTL window serves both this poller and the REST
       `/api/proposals` handler that the same dashboard hits on re-mount.
       Worst-case latency for a newly-scheduler-created proposal to appear:
       TTL + one 3 s tick ≈ 13 s.
    """
    from api.proposals_cache import current_version, get_pending_with_version

    loop = asyncio.get_running_loop()
    repo = await loop.run_in_executor(None, get_repo)

    seen_ids: set[int] = set()
    # Start as "idle" so the first active tick (right after lifespan startup,
    # with or without a connected client) goes through the silent-seed path —
    # we must not broadcast existing proposals when nothing has been set up yet.
    was_idle: bool = True
    # Track the last version of the proposals cache we observed; bumps via
    # invalidate() signal that the cached row set may have changed in a way
    # the simple seen_ids diff can't represent — most notably a proposal
    # whose approval was rolled back to PENDING (broker failure) after we'd
    # already broadcast it once. Without this, the rolled-back proposal
    # would silently drop out of the WS feed.
    # Capture at startup so the first tick doesn't treat itself as a
    # version bump (silent-seeds on startup and reconnect-from-idle).
    last_version: int = current_version()
    while True:
        try:
            if manager._active:
                # Atomic (result, version) reader. Returning the version
                # alongside the data inside get_pending_with_version avoids
                # the TOCTOU window where invalidate() could land between
                # the data fetch (in run_in_executor) and a separate
                # current_version() read on the asyncio loop.
                proposals, cur_version = await loop.run_in_executor(
                    None, get_pending_with_version, repo
                )
                if cur_version != last_version:
                    # State changed in DB — clear seen_ids so the rolled-back
                    # proposal gets re-broadcast, alongside any genuinely-new
                    # ones that arrived since. Cost: a small extra WS broadcast
                    # burst on each approve / reject / rollback. Acceptable
                    # because humans are the only trigger for these.
                    seen_ids.clear()
                    last_version = cur_version
                    was_idle = False
                elif was_idle:
                    # Reconnect (or startup with active client) — seed without broadcasting.
                    seen_ids = {p["id"] for p in proposals}
                    was_idle = False
                else:
                    # Drop ids that left PENDING (approved/rejected/executed).
                    seen_ids &= {p["id"] for p in proposals}
                new = [p for p in proposals if p["id"] not in seen_ids]
                for p in new:
                    seen_ids.add(p["id"])
                    await manager.broadcast({"event": "new_proposal", "data": p})
            else:
                # No clients — don't poll. Mark idle so the next active tick
                # treats itself as a reconnect and seeds `seen_ids` afresh.
                was_idle = True
        except Exception:
            logger.exception("proposal_poller error — will retry")
        await asyncio.sleep(3)


async def ws_handler(websocket: WebSocket) -> None:
    """WebSocket endpoint — auth-gated, auto-reconnect friendly.

    Token arrives as a query param, not an Authorization header — the browser
    WebSocket API can't set custom headers on the handshake.
    """
    token_exp: float | None = None
    if auth_enabled():
        token = websocket.query_params.get("token")
        if not token:
            await websocket.close(code=1008)
            return
        try:
            claims = verify_supabase_jwt(token)
        except jwt.PyJWTError:
            await websocket.close(code=1008)
            return
        # The socket outlives the request that authorized it; close it when the
        # token expires instead of trusting a stale JWT for the connection's lifetime.
        token_exp = claims.get("exp")

    await manager.connect(websocket)
    try:
        # Keep connection alive; client may send pings.
        while True:
            if token_exp is not None:
                remaining = token_exp - time.time()
                if remaining <= 0:
                    await websocket.close(code=1008)
                    manager.disconnect(websocket)
                    return
                # Wake at expiry even if the client stays silent.
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
            else:
                await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
