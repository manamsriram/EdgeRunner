"""Rate limiter: allows up to the cap, blocks past it, and recovers after the window."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from api.routes import analysis


@pytest.fixture(autouse=True)
def _clear_hits():
    analysis._hits.clear()
    yield
    analysis._hits.clear()


def test_allows_up_to_limit():
    for _ in range(analysis._RATE_LIMIT):
        analysis._check_rate_limit("1.2.3.4")  # should not raise


def test_blocks_past_limit():
    for _ in range(analysis._RATE_LIMIT):
        analysis._check_rate_limit("1.2.3.4")
    with pytest.raises(HTTPException) as exc:
        analysis._check_rate_limit("1.2.3.4")
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers


def test_ips_are_independent():
    for _ in range(analysis._RATE_LIMIT):
        analysis._check_rate_limit("1.2.3.4")
    analysis._check_rate_limit("5.6.7.8")  # different IP, should not raise


def test_recovers_after_window(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(analysis.time, "monotonic", lambda: t[0])
    for _ in range(analysis._RATE_LIMIT):
        analysis._check_rate_limit("1.2.3.4")
    with pytest.raises(HTTPException):
        analysis._check_rate_limit("1.2.3.4")
    t[0] += analysis._RATE_WINDOW_S + 1
    analysis._check_rate_limit("1.2.3.4")  # window expired, should not raise
