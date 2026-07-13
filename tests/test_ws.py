"""WebSocket auth lifecycle: a connection must not outlive the JWT that authorized it."""
from __future__ import annotations

import asyncio
import time

from fastapi import WebSocketDisconnect

from api import ws


class _FakeWS:
    def __init__(self, exp: float, *, disconnect_after: bool = False):
        self.query_params = {"token": "t"}
        self._claims_exp = exp
        self.closed: int | None = None
        self.disconnect_after = disconnect_after

    async def accept(self):
        pass

    async def close(self, code: int):
        self.closed = code

    async def receive_text(self):
        if self.disconnect_after:
            raise WebSocketDisconnect()
        await asyncio.sleep(0.01)
        return "ping"


def _patch(monkeypatch, exp: float):
    monkeypatch.setattr(ws, "auth_enabled", lambda: True)
    monkeypatch.setattr(ws, "verify_supabase_jwt", lambda t: {"exp": exp})


def test_expired_token_closes_connection(monkeypatch):
    _patch(monkeypatch, exp=time.time() - 1)
    fake = _FakeWS(exp=time.time() - 1)
    asyncio.run(ws.ws_handler(fake))
    assert fake.closed == 1008


def test_fresh_token_stays_open(monkeypatch):
    _patch(monkeypatch, exp=time.time() + 3600)
    fake = _FakeWS(exp=time.time() + 3600, disconnect_after=True)
    asyncio.run(ws.ws_handler(fake))
    assert fake.closed is None  # closed by client disconnect, not by expiry
