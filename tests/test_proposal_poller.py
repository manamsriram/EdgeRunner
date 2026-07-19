"""Behavior tests for proposal_poller: idle-egress savings, silent-seed on
startup/reconnect, forward-diff broadcasts, seen-ids eviction on approval,
and the broker-failure-rollback regression.

The poller is an infinite loop driven by asyncio.sleep(3). Each test
monkeypatches asyncio.sleep to count iterations and cancel after N.
`_TTL = 0.0` is set so every tick refetches — call_count then equals
the iteration count, which is what these tests assert against.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from api import proposals_cache
from api import ws as ws_module


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset shared state so tests don't bleed into each other."""
    proposals_cache._reset_for_tests()
    ws_module.manager._active.clear()
    yield
    ws_module.manager._active.clear()
    proposals_cache._reset_for_tests()


def _run_poller(monkeypatch, *, repo, max_ticks, between_ticks=None):
    """Run proposal_poller() for `max_ticks` iterations, then cancel.

    `between_ticks[i]` is invoked AFTER iteration i+1 finishes its sleep and
    BEFORE iteration i+2 begins. Use it to mutate `manager._active`, change
    `repo.list_pending_proposals.return_value`, or call `proposals_cache.invalidate()`
    between iterations.

    Returns (broadcasts, ticks_run).
    """
    broadcasts: list = []

    async def fake_broadcast(msg):
        broadcasts.append(msg)
    monkeypatch.setattr(ws_module.manager, "broadcast", fake_broadcast)
    monkeypatch.setattr("api.ws.get_repo", lambda: repo)
    # Force every get_pending() to refetch — tests assert tick count via
    # repo.list_pending_proposals.call_count, which only matches max_ticks
    # when the cache never serves a hit.
    monkeypatch.setattr(proposals_cache, "_TTL", 0.0)

    counter = {"n": 0}
    real_sleep = asyncio.sleep

    async def counting_sleep(_):
        counter["n"] += 1
        if between_ticks is not None and counter["n"] - 1 < len(between_ticks):
            between_ticks[counter["n"] - 1]()
        if counter["n"] >= max_ticks:
            raise asyncio.CancelledError()
        await real_sleep(0)  # yield without burning a real second
    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    async def runner():
        try:
            await ws_module.proposal_poller()
        except asyncio.CancelledError:
            pass

    asyncio.run(runner())
    return broadcasts, counter["n"]


def test_poller_does_not_query_db_when_no_clients(monkeypatch):
    """Idle hours / closed tabs must not pay Supabase egress for fresh
    proposal lists nobody is going to broadcast."""
    repo = MagicMock()
    repo.list_pending_proposals.return_value = []
    broadcasts, _ = _run_poller(monkeypatch, repo=repo, max_ticks=3)
    assert repo.list_pending_proposals.call_count == 0
    assert broadcasts == []


def test_poller_first_active_tick_starts_with_silent_seed(monkeypatch):
    """The very first active tick (startup or reconnect-from-idle path) must
    seed `seen_ids` from the current pending list WITHOUT broadcasting — the
    dashboard fetched that list via REST on initial page load."""
    ws_module.manager._active.add(MagicMock())
    repo = MagicMock()
    repo.list_pending_proposals.return_value = [{"id": 1, "symbol": "AAPL"}]
    broadcasts, _ = _run_poller(monkeypatch, repo=repo, max_ticks=2)
    # Tick 1: silent seed; tick 2: same data, no version bump → no broadcast.
    assert repo.list_pending_proposals.call_count == 2
    assert broadcasts == []


def test_poller_does_not_rebroadcast_unchanged_proposals(monkeypatch):
    """Steady-state active tick: same proposals as last tick → no broadcast."""
    ws_module.manager._active.add(MagicMock())
    repo = MagicMock()
    repo.list_pending_proposals.return_value = [{"id": 1, "symbol": "AAPL"}]
    broadcasts, _ = _run_poller(monkeypatch, repo=repo, max_ticks=3)
    assert repo.list_pending_proposals.call_count == 3
    assert broadcasts == []


def test_poller_broadcasts_only_genuinely_new_proposal(monkeypatch):
    """A proposal that wasn't in seen_ids on the previous tick must be
    broadcast; previously-seen ones must not be re-broadcast."""
    ws_module.manager._active.add(MagicMock())
    repo = MagicMock()
    repo.list_pending_proposals.return_value = [{"id": 1}]

    def add_proposal_2():
        repo.list_pending_proposals.return_value = [{"id": 1}, {"id": 2}]

    def add_proposal_3():
        repo.list_pending_proposals.return_value = [{"id": 1}, {"id": 2}, {"id": 3}]

    broadcasts, _ = _run_poller(
        monkeypatch, repo=repo, max_ticks=3,
        between_ticks=[add_proposal_2, add_proposal_3],
    )
    assert repo.list_pending_proposals.call_count == 3
    assert [b["data"]["id"] for b in broadcasts] == [2, 3]


def test_poller_drops_ids_that_left_pending_so_they_dont_pin(monkeypatch):
    """If a proposal is approved/rejected between ticks (no longer in
    pending), its id must be evicted from seen_ids so a re-appearance is
    treated as new."""
    ws_module.manager._active.add(MagicMock())
    repo = MagicMock()
    repo.list_pending_proposals.return_value = [{"id": 1}, {"id": 2}]

    def remove_proposal_1():
        repo.list_pending_proposals.return_value = [{"id": 2}]

    def re_add_proposal_1():
        repo.list_pending_proposals.return_value = [{"id": 2}, {"id": 1}]

    broadcasts, _ = _run_poller(
        monkeypatch, repo=repo, max_ticks=3,
        between_ticks=[remove_proposal_1, re_add_proposal_1],
    )
    # Tick 1: silent seed (was_idle=True) → seen_ids={1,2}, no broadcast.
    # Tick 2: proposals=[2] → seen_ids &= {2} → seen_ids={2}, no broadcast.
    # Tick 3: proposals=[2,1] → id=1 not in {2} → broadcast id=1.
    assert repo.list_pending_proposals.call_count == 3
    assert [b["data"]["id"] for b in broadcasts] == [1]


def test_poller_reconnect_after_disconnect_silently_reseeds(monkeypatch):
    """When all clients disconnect and a new one connects, the poller must NOT
    re-broadcast existing proposals — the freshly-arrived dashboard fetched
    them via REST on initial page load."""
    fake_ws = MagicMock()
    ws_module.manager._active.add(fake_ws)
    repo = MagicMock()
    repo.list_pending_proposals.return_value = [
        {"id": 1, "symbol": "AAPL"},
        {"id": 2, "symbol": "MSFT"},
    ]

    def disconnect():
        ws_module.manager._active.clear()

    def reconnect():
        ws_module.manager._active.add(fake_ws)

    broadcasts, _ = _run_poller(
        monkeypatch, repo=repo, max_ticks=5,
        # After tick 1: disconnect. After tick 2: idle tick skipped. After tick 3: reconnect.
        between_ticks=[disconnect, lambda: None, reconnect],
    )
    # 4 active ticks: seed (no broadcast), no-new, idle (no DB), reseed (silent).
    # 5 sleeps total: disconnect, idle, reconnect, after-tick-4, cancel.
    # DB hit 3 times (tick 1, 2, 4) — no broadcasts ever.
    assert repo.list_pending_proposals.call_count == 3
    assert broadcasts == []


def test_poller_rebroadcasts_after_invalidate_covers_broker_rollback(monkeypatch):
    """Regression: a broker-failure rollback in /api/proposals/approve sets
    status back to PENDING and calls invalidate(). The simple seen_ids diff
    cannot represent a PENDING→APPROVED→PENDING flip (the id leaves and
    re-enters the list while still being remembered as "broadcasted");
    without the version counter in api.proposals_cache, the rolled-back
    proposal would silently drop from the WS feed and the dashboard would
    keep showing it as "approve in flight" until the next manual refresh.
    """
    fake_ws = MagicMock()
    ws_module.manager._active.add(fake_ws)
    repo = MagicMock()
    repo.list_pending_proposals.return_value = [{"id": 5}]

    def rollback_invalidate():
        # Mirrors api/routes/proposals.py::approve broker-failure branch:
        # set_proposal_status(id, PENDING); invalidate().
        proposals_cache.invalidate()

    broadcasts, _ = _run_poller(
        monkeypatch, repo=repo, max_ticks=4,
        # After tick 2: simulate the broker-failure rollback invalidate().
        between_ticks=[lambda: None, rollback_invalidate],
    )
    # Tick 1: silent seed → seen_ids={5}, no broadcast.
    # Tick 2: same data, no version bump → no broadcast.
    # After tick 2: invalidate() bumps version to 1.
    # Tick 3: version bump → clear seen_ids → broadcast id=5 again.
    assert [b["data"]["id"] for b in broadcasts] == [5]
