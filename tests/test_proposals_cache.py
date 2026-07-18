"""Unit tests for the in-process TTL cache around list_pending_proposals().

Pure behavior — no asyncio, no DB. The point of sharing the cache with the WS
poller is that concurrent readers coalesce into one DB query per TTL window;
the test below locks that contract in.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from api import proposals_cache


@pytest.fixture(autouse=True)
def _reset_cache_and_shorten_ttl(monkeypatch):
    """Each test starts clean; TTL is shrunk so the "after TTL" test runs fast."""
    proposals_cache._reset_for_tests()
    monkeypatch.setattr(proposals_cache, "_TTL", 0.05)
    yield
    proposals_cache._reset_for_tests()


def test_cache_miss_fetches_from_repo():
    repo = MagicMock()
    repo.list_pending_proposals.return_value = [{"id": 1}]
    assert proposals_cache.get_pending(repo) == [{"id": 1}]
    assert repo.list_pending_proposals.call_count == 1


def test_cache_hit_within_ttl_does_not_refetch():
    repo = MagicMock()
    repo.list_pending_proposals.return_value = [{"id": 1}]
    proposals_cache.get_pending(repo)
    proposals_cache.get_pending(repo)
    proposals_cache.get_pending(repo)
    assert repo.list_pending_proposals.call_count == 1


def test_cache_refreshes_after_ttl_expires():
    repo = MagicMock()
    repo.list_pending_proposals.side_effect = [[{"id": 1}], [{"id": 2}]]
    assert proposals_cache.get_pending(repo) == [{"id": 1}]
    time.sleep(0.06)  # > _TTL (0.05)
    assert proposals_cache.get_pending(repo) == [{"id": 2}]
    assert repo.list_pending_proposals.call_count == 2


def test_invalidate_forces_refetch():
    repo = MagicMock()
    repo.list_pending_proposals.side_effect = [[{"id": 1}], [{"id": 2}]]
    assert proposals_cache.get_pending(repo) == [{"id": 1}]
    proposals_cache.invalidate()
    assert proposals_cache.get_pending(repo) == [{"id": 2}]
    assert repo.list_pending_proposals.call_count == 2


def test_shared_cache_returns_same_reference_to_concurrent_readers():
    """Two callers within the TTL window must share a single DB fetch and get
    the same list reference (no copy) — that's the point of sharing it with
    the WS poller."""
    repo = MagicMock()
    repo.list_pending_proposals.return_value = [{"id": 1}]
    a = proposals_cache.get_pending(repo)
    b = proposals_cache.get_pending(repo)
    assert a is b
    assert repo.list_pending_proposals.call_count == 1
