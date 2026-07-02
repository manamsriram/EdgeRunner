"""Unit tests for trader.performance.metrics.

No network, no Alpaca keys — all external calls are injected via mock broker/repo.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from trader.performance.metrics import (
    LiveMetrics,
    _check_thresholds,
    _fifo_round_trips,
    _profit_factor,
    compute_live_metrics,
)


# ---- helpers ----

def _timestamps(n: int) -> list[str]:
    base = date(2026, 1, 1)
    return [(base + timedelta(days=i)).isoformat() + "T00:00:00" for i in range(n)]


def _equity(n: int, start: float = 100_000.0, drift: float = 50.0) -> list[float]:
    """Rising equity curve with enough variance to produce a non-zero Sharpe."""
    import random
    random.seed(42)
    vals = [start]
    for _ in range(n - 1):
        vals.append(vals[-1] + drift + random.gauss(0, 200))
    return vals


def _make_broker(history=None, fills=None):
    broker = MagicMock()
    broker.get_portfolio_history.return_value = history
    broker.get_account_activities.return_value = fills or []
    return broker


def _make_repo(signal_counts=None, orders=None):
    repo = MagicMock()
    repo.get_strategy_signal_counts.return_value = signal_counts or {}
    repo.get_orders.return_value = orders or []
    return repo


def _make_config():
    return MagicMock()


# ---- _fifo_round_trips ----

def test_fifo_simple_win():
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 1.0, "price": 100.0, "ts": "2026-01-01"},
        {"symbol": "AAPL", "side": "sell", "qty": 1.0, "price": 110.0, "ts": "2026-01-02"},
    ]
    assert _fifo_round_trips(fills) == pytest.approx([10.0])


def test_fifo_simple_loss():
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 2.0, "price": 100.0, "ts": "2026-01-01"},
        {"symbol": "AAPL", "side": "sell", "qty": 2.0, "price":  90.0, "ts": "2026-01-02"},
    ]
    assert _fifo_round_trips(fills) == pytest.approx([-20.0])


def test_fifo_partial_sell_excludes_open():
    """Only the sold portion is counted; remaining open position is excluded."""
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 3.0, "price": 100.0, "ts": "2026-01-01"},
        {"symbol": "AAPL", "side": "sell", "qty": 1.0, "price": 110.0, "ts": "2026-01-02"},
    ]
    pnls = _fifo_round_trips(fills)
    assert len(pnls) == 1
    assert pnls[0] == pytest.approx(10.0)


def test_fifo_multiple_symbols_independent():
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 1.0, "price": 100.0, "ts": "2026-01-01"},
        {"symbol": "MSFT", "side": "buy",  "qty": 1.0, "price": 200.0, "ts": "2026-01-01"},
        {"symbol": "AAPL", "side": "sell", "qty": 1.0, "price": 105.0, "ts": "2026-01-02"},
        {"symbol": "MSFT", "side": "sell", "qty": 1.0, "price": 195.0, "ts": "2026-01-02"},
    ]
    pnls = _fifo_round_trips(fills)
    assert len(pnls) == 2
    assert pytest.approx(5.0) in pnls
    assert pytest.approx(-5.0) in pnls


def test_fifo_only_buys_returns_empty():
    fills = [{"symbol": "AAPL", "side": "buy", "qty": 1.0, "price": 100.0, "ts": "2026-01-01"}]
    assert _fifo_round_trips(fills) == []


def test_fifo_empty_returns_empty():
    assert _fifo_round_trips([]) == []


def test_fifo_fifo_ordering():
    """Second buy at higher price; first lot should match first sell."""
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 1.0, "price": 100.0, "ts": "2026-01-01"},
        {"symbol": "AAPL", "side": "buy",  "qty": 1.0, "price": 120.0, "ts": "2026-01-02"},
        {"symbol": "AAPL", "side": "sell", "qty": 1.0, "price": 115.0, "ts": "2026-01-03"},
    ]
    pnls = _fifo_round_trips(fills)
    assert len(pnls) == 1
    assert pnls[0] == pytest.approx(15.0)  # matched against first buy at 100


# ---- _profit_factor ----

def test_profit_factor_mixed():
    pnls = [10.0, -5.0, 8.0, -3.0]
    assert _profit_factor(pnls) == pytest.approx(18.0 / 8.0)


def test_profit_factor_all_wins_returns_inf():
    assert _profit_factor([10.0, 5.0]) == float("inf")


def test_profit_factor_all_losses_returns_zero():
    assert _profit_factor([-10.0, -5.0]) == 0.0


def test_profit_factor_no_trades_returns_zero():
    assert _profit_factor([]) == 0.0


# ---- _check_thresholds ----

def _passing():
    return dict(
        days_active=61, trade_count=101, sharpe=1.1,
        max_drawdown=-0.10, win_rate=0.50, profit_factor=1.6,
    )


def test_check_thresholds_all_pass():
    assert _check_thresholds(**_passing()) == []


def test_check_thresholds_sharpe_fail():
    kw = {**_passing(), "sharpe": 0.5}
    failures = _check_thresholds(**kw)
    assert any("Sharpe" in f for f in failures)


def test_check_thresholds_drawdown_fail():
    kw = {**_passing(), "max_drawdown": -0.20}
    failures = _check_thresholds(**kw)
    assert any("drawdown" in f for f in failures)


def test_check_thresholds_days_fail():
    kw = {**_passing(), "days_active": 30}
    failures = _check_thresholds(**kw)
    assert any("days" in f for f in failures)


def test_check_thresholds_trades_fail():
    kw = {**_passing(), "trade_count": 5}
    failures = _check_thresholds(**kw)
    assert any("round-trips" in f for f in failures)


def test_check_thresholds_win_rate_fail():
    kw = {**_passing(), "win_rate": 0.30}
    failures = _check_thresholds(**kw)
    assert any("win rate" in f for f in failures)


def test_check_thresholds_profit_factor_fail():
    kw = {**_passing(), "profit_factor": 1.1}
    failures = _check_thresholds(**kw)
    assert any("profit factor" in f for f in failures)


def test_check_thresholds_profit_factor_inf_passes():
    """Infinite profit factor (all wins) must not trigger a failure."""
    kw = {**_passing(), "profit_factor": float("inf")}
    assert _check_thresholds(**kw) == []


def test_check_thresholds_multiple_failures():
    kw = {**_passing(), "sharpe": 0.3, "win_rate": 0.30}
    assert len(_check_thresholds(**kw)) >= 2


# ---- compute_live_metrics ----

def test_compute_insufficient_data_no_history():
    broker = _make_broker(history=None)
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.verdict == "INSUFFICIENT_DATA"
    assert result.days_active == 0


def test_compute_insufficient_data_single_equity_point():
    broker = _make_broker(
        history={"equity": [100_000.0], "timestamp": ["2026-01-01T00:00:00"]}
    )
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.verdict == "INSUFFICIENT_DATA"


def test_compute_fail_no_trades():
    n = 90
    broker = _make_broker(
        history={"equity": _equity(n), "timestamp": _timestamps(n)},
        fills=[],
    )
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.verdict == "FAIL"
    assert result.trade_count == 0
    assert any("round-trips" in f for f in result.failing_checks)


def test_compute_strategy_signals_passed_through():
    n = 90
    broker = _make_broker(
        history={"equity": _equity(n), "timestamp": _timestamps(n)},
        fills=[],
    )
    repo = _make_repo(signal_counts={"MomentumRSI": 42, "MACrossover": 18})
    result = compute_live_metrics(_make_config(), broker, repo)
    assert result.strategy_signals == {"MomentumRSI": 42, "MACrossover": 18}


def test_compute_metrics_populated_from_equity_curve():
    n = 90
    broker = _make_broker(
        history={"equity": _equity(n), "timestamp": _timestamps(n)},
        fills=[],
    )
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.days_active == n - 1
    assert isinstance(result.sharpe, float)
    assert result.max_drawdown <= 0.0


def test_compute_profit_factor_from_fills():
    n = 90
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 1.0, "price": 100.0, "ts": "2026-01-01T10:00:00"},
        {"symbol": "AAPL", "side": "sell", "qty": 1.0, "price": 110.0, "ts": "2026-01-02T10:00:00"},
    ]
    broker = _make_broker(
        history={"equity": _equity(n), "timestamp": _timestamps(n)},
        fills=fills,
    )
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.trade_count == 1
    assert result.win_rate == pytest.approx(1.0)
    assert result.profit_factor == float("inf")


def test_compute_days_active_uses_local_orders_when_fills_endpoint_is_empty():
    """Regression: if get_account_activities silently fails (e.g. a page_size bug),
    fills == [] must not force days_active back to the full 1-year history window —
    the local order ledger is an independent source of the bot's real start date."""
    n = 90
    timestamps = _timestamps(n)
    broker = _make_broker(
        history={"equity": _equity(n), "timestamp": timestamps},
        fills=[],  # simulates a broken/empty Alpaca activities response
    )
    # Bot's first-ever order was recorded locally 5 days before the history ends.
    first_order_ts = timestamps[n - 5]
    repo = _make_repo(orders=[{"ts": first_order_ts, "symbol": "AAPL"}])

    result = compute_live_metrics(_make_config(), broker, repo)
    assert result.days_active <= 5
    assert result.days_active < n - 1


def test_compute_benchmark_none_when_fetch_fails(monkeypatch):
    """Benchmark failure must not block verdict computation."""
    import trader.performance.metrics as m
    monkeypatch.setattr(m, "_benchmark_return", lambda *a, **kw: None)
    n = 90
    broker = _make_broker(
        history={"equity": _equity(n), "timestamp": _timestamps(n)},
        fills=[],
    )
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.benchmark_spy_return is None
    assert result.benchmark_btc_return is None
    assert result.verdict in ("PASS", "FAIL")  # not INSUFFICIENT_DATA
