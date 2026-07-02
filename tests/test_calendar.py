"""Unit tests for trader.performance.calendar.

Regression coverage for the day-bucketing off-by-one bug (commit 1b2ea81
mis-assigned each day's P&L to the *prior* day's tile).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from trader.performance.calendar import compute_calendar_data


def _make_broker(equity: list[float], timestamps: list[str]):
    broker = MagicMock()
    broker.get_portfolio_history.return_value = {
        "equity": equity,
        "timestamp": timestamps,
    }
    broker.get_account_activities.return_value = []
    return broker


def _make_repo():
    repo = MagicMock()
    repo.get_orders.return_value = []
    return repo


def test_daily_pnl_assigned_to_own_day_not_prior_day():
    """Day N's equity delta (close[N] - close[N-1]) must land on day N's tile,
    not day N-1's — regression test for the off-by-one bucketing bug."""
    equity = [100_000.0, 100_500.0, 99_800.0]
    timestamps = [
        "2026-06-30T20:00:00Z",  # Tue close
        "2026-07-01T20:00:00Z",  # Wed close: +500 vs Tue
        "2026-07-02T20:00:00Z",  # Thu close: -700 vs Wed
    ]
    broker = _make_broker(equity, timestamps)
    result = compute_calendar_data(broker, _make_repo())
    by_date = {row["date"]: row for row in result}

    assert by_date["2026-07-01"]["pnl_amount"] == 500.0
    assert by_date["2026-07-02"]["pnl_amount"] == -700.0
    # The Tuesday close has no prior bar to diff against, so it never gets its
    # own delta — confirms the fix didn't shift dates, just corrected direction.
    assert "2026-06-30" not in by_date
