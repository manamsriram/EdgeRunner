"""Unit tests for trader.learning.update_weights — nightly bandit batch logic.

Uses a real in-memory SQLiteRepository so we verify storage round-trips, not mocks.
The broker is not needed: fills are passed in as plain dicts.
"""
from __future__ import annotations

import pytest

from trader.learning.bandit_weights import DEFAULT_WEIGHT
from trader.learning.update_weights import compute_pnls_from_fills, update_bandit_weights
from trader.portfolio.repository import OrderRow
from trader.portfolio.sqlite_repo import SQLiteRepository


@pytest.fixture
def repo(tmp_path) -> SQLiteRepository:
    return SQLiteRepository(str(tmp_path / "portfolio.db"))


def _seed_order(repo, broker_order_id, strategy, regime, symbol="AAPL", side="buy"):
    repo.record_order(OrderRow(
        client_order_id=f"c-{broker_order_id}",
        symbol=symbol,
        side=side,
        notional=1000.0,
        status="accepted",
        broker_order_id=broker_order_id,
        strategy_name=strategy,
        regime=regime,
    ))


# ---- compute_pnls_from_fills ----

def test_no_fills_returns_empty():
    assert compute_pnls_from_fills(orders=[], fills=[]) == {}


def test_fills_without_matching_orders_are_skipped():
    fills = [{"order_id": "b1", "symbol": "AAPL", "side": "buy", "qty": 10, "price": 100.0}]
    result = compute_pnls_from_fills(orders=[], fills=fills)
    assert result == {}


def test_buy_fill_alone_produces_no_pnl():
    orders = [{"broker_order_id": "b1", "strategy_name": "DonchianBreakout",
               "regime": "calm", "symbol": "AAPL"}]
    fills = [{"order_id": "b1", "symbol": "AAPL", "side": "buy", "qty": 10, "price": 100.0}]
    result = compute_pnls_from_fills(orders=orders, fills=fills)
    assert result == {}


def test_matched_profitable_fill_produces_positive_pnl():
    orders = [
        {"broker_order_id": "b1", "strategy_name": "DonchianBreakout",
         "regime": "calm", "symbol": "AAPL"},
        {"broker_order_id": "s1", "strategy_name": "DonchianBreakout",
         "regime": "calm", "symbol": "AAPL"},
    ]
    fills = [
        {"order_id": "b1", "symbol": "AAPL", "side": "buy",  "qty": 10, "price": 100.0},
        {"order_id": "s1", "symbol": "AAPL", "side": "sell", "qty": 10, "price": 120.0},
    ]
    result = compute_pnls_from_fills(orders=orders, fills=fills)
    assert ("DonchianBreakout", "calm") in result
    assert result[("DonchianBreakout", "calm")] == [pytest.approx(200.0)]


def test_matched_losing_fill_produces_negative_pnl():
    orders = [
        {"broker_order_id": "b1", "strategy_name": "SuperTrend",
         "regime": "trending", "symbol": "TSLA"},
        {"broker_order_id": "s1", "strategy_name": "SuperTrend",
         "regime": "trending", "symbol": "TSLA"},
    ]
    fills = [
        {"order_id": "b1", "symbol": "TSLA", "side": "buy",  "qty": 5, "price": 200.0},
        {"order_id": "s1", "symbol": "TSLA", "side": "sell", "qty": 5, "price": 180.0},
    ]
    result = compute_pnls_from_fills(orders=orders, fills=fills)
    assert result[("SuperTrend", "trending")] == [pytest.approx(-100.0)]


def test_multiple_strategies_bucketed_separately():
    orders = [
        {"broker_order_id": "b1", "strategy_name": "A", "regime": "calm", "symbol": "AAPL"},
        {"broker_order_id": "s1", "strategy_name": "A", "regime": "calm", "symbol": "AAPL"},
        {"broker_order_id": "b2", "strategy_name": "B", "regime": "calm", "symbol": "AAPL"},
        {"broker_order_id": "s2", "strategy_name": "B", "regime": "calm", "symbol": "AAPL"},
    ]
    fills = [
        {"order_id": "b1", "symbol": "AAPL", "side": "buy",  "qty": 10, "price": 100.0},
        {"order_id": "s1", "symbol": "AAPL", "side": "sell", "qty": 10, "price": 110.0},
        {"order_id": "b2", "symbol": "AAPL", "side": "buy",  "qty": 10, "price": 100.0},
        {"order_id": "s2", "symbol": "AAPL", "side": "sell", "qty": 10, "price":  90.0},
    ]
    result = compute_pnls_from_fills(orders=orders, fills=fills)
    assert result[("A", "calm")] == [pytest.approx(100.0)]
    assert result[("B", "calm")] == [pytest.approx(-100.0)]


# ---- update_bandit_weights ----

def _make_profitable_fills(repo, strategy, regime, symbol, n=20):
    """Seed n profitable round-trips in repo + matching fills list."""
    orders = []
    fills = []
    for i in range(n):
        bid = f"b{strategy}{i}"
        sid = f"s{strategy}{i}"
        _seed_order(repo, bid, strategy, regime, symbol=symbol, side="buy")
        _seed_order(repo, sid, strategy, regime, symbol=symbol, side="sell")
        orders.append({"broker_order_id": bid, "strategy_name": strategy,
                       "regime": regime, "symbol": symbol})
        orders.append({"broker_order_id": sid, "strategy_name": strategy,
                       "regime": regime, "symbol": symbol})
        fills.append({"order_id": bid, "symbol": symbol, "side": "buy",
                      "qty": 10, "price": 100.0})
        fills.append({"order_id": sid, "symbol": symbol, "side": "sell",
                      "qty": 10, "price": 120.0})
    return fills


def test_update_with_no_fills_writes_nothing(repo):
    weights = update_bandit_weights(repo, fills=[], cycle_index=1)
    assert weights == {}
    assert repo.get_all_bandit_weights() == {}


def test_update_with_few_fills_still_updates(repo):
    # Thompson sampling has no min_samples floor — updates even with 1 round-trip
    fills = _make_profitable_fills(repo, "DonchianBreakout", "calm", "AAPL", n=1)
    weights = update_bandit_weights(repo, fills=fills, cycle_index=1)
    assert ("DonchianBreakout", "calm") in weights


def test_update_with_profitable_fills_raises_weight(repo):
    fills = _make_profitable_fills(repo, "DonchianBreakout", "calm", "AAPL", n=20)
    weights = update_bandit_weights(repo, fills=fills, cycle_index=1)
    arm = ("DonchianBreakout", "calm")
    assert arm in weights
    # After 20 wins, arm should have alpha=21, beta=1 → weight skews high
    arms = repo.get_all_bandit_arms()
    assert arms[arm][0] == 21  # alpha_wins
    assert arms[arm][1] == 1   # beta_losses
    # Weight should be > DEFAULT_WEIGHT given 20 wins and 0 losses
    stored_weight = repo.get_bandit_weight("DonchianBreakout", "calm")
    assert stored_weight > DEFAULT_WEIGHT


def test_update_forced_exploration_resets_to_default(repo):
    repo.save_bandit_arm("DonchianBreakout", "calm", 50, 5, 9, 1.45)
    fills = _make_profitable_fills(repo, "DonchianBreakout", "calm", "AAPL", n=5)
    weights = update_bandit_weights(repo, fills=fills, cycle_index=10, every=10)
    assert weights[("DonchianBreakout", "calm")] == DEFAULT_WEIGHT
    # Counts reset to (1, 1)
    arms = repo.get_all_bandit_arms()
    assert arms[("DonchianBreakout", "calm")] == (1, 1, 10)


def test_bandit_arm_persisted_after_update(repo):
    fills = _make_profitable_fills(repo, "SuperTrend", "normal", "AAPL", n=5)
    update_bandit_weights(repo, fills=fills, cycle_index=1)
    arms = repo.get_all_bandit_arms()
    assert ("SuperTrend", "normal") in arms
    alpha, beta, _ = arms[("SuperTrend", "normal")]
    assert alpha >= 1 and beta >= 1
