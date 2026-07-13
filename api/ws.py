"""WebSocket broadcaster + DB-polling background task.

No IPC with the scheduler process required — we poll the proposals table every 3 seconds
and broadcast diffs to connected clients. 3-second latency is acceptable vs. the current
"click Refresh" model.
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
    """Background task: polls DB every 3s; broadcasts newly created pending proposals."""
    loop = asyncio.get_running_loop()
    repo = await loop.run_in_executor(None, get_repo)
    seen_ids: set[int] = set()
    while True:
        try:
            # Run via executor — psycopg2.connect() is blocking; calling it directly
            # on the event loop stalls health-check responses and causes Render restarts.
            proposals = await loop.run_in_executor(None, repo.list_pending_proposals)
            seen_ids &= {p["id"] for p in proposals}  # evict approved/rejected proposals
            new = [p for p in proposals if p["id"] not in seen_ids]
            for p in new:
                seen_ids.add(p["id"])
                await manager.broadcast({"event": "new_proposal", "data": p})
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
