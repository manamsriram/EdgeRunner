"""Scheduler tests: clock gate, kill switch gate, pipeline delegation.

No network. The broker's TradingClient is injected with a fake that controls
the clock response and account state.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from trader.config import Config, RiskLimits
from trader.execution.broker import AlpacaBroker
from trader.portfolio.sqlite_repo import SQLiteRepository
from trader.risk.gate import AccountState, KillSwitch
from trader.scheduler import is_market_open, run_nightly_bandit_update, run_once
from trader.strategy.base import Signal, Strategy


class FakeClockClient:
    """TradingClient stand-in that controls clock.is_open."""

    def __init__(self, *, market_open: bool = True, clock_error: bool = False) -> None:
        self._market_open = market_open
        self._clock_error = clock_error

    def get_clock(self):
        if self._clock_error:
            raise RuntimeError("clock API down")
        return SimpleNamespace(is_open=self._market_open)

    def get_account(self):
        return SimpleNamespace(equity="100000", last_equity="100000")

    def get_all_positions(self):
        return []

    def get_orders(self, filter):  # noqa: A002
        return []

    def submit_order(self, order_data: dict) -> Any:
        return SimpleNamespace(id="x", **order_data)

    def get_order_by_client_id(self, client_id: str) -> Any:
        raise KeyError(client_id)


def _config(ks_path: str = "ks.flag", db_path: str = "portfolio.db") -> Config:
    return Config(
        alpaca_api_key="k", alpaca_secret_key="s", alpaca_paper=True,
        autonomy="manual", openai_api_key=None, anthropic_api_key=None,
        portfolio_db_path=db_path, kill_switch_path=ks_path,
        risk=RiskLimits(allowlist=("AAPL",)),
    )


def _broker(*, market_open: bool = True, clock_error: bool = False) -> AlpacaBroker:
    cfg = _config()
    client = FakeClockClient(market_open=market_open, clock_error=clock_error)
    return AlpacaBroker(
        cfg,
        client=client,
        request_builder=lambda **kw: kw,
        order_filter_builder=lambda today: ("open", "closed"),
    )


class _NeverCalledStrategy(Strategy):
    """Raises if generate() is called — used to assert pipeline was not entered."""

    def _decide(self, bars, asof) -> Signal:
        raise AssertionError("pipeline should not have been called")


# ---- is_market_open ----

def test_is_market_open_true():
    assert is_market_open(_broker(market_open=True)) is True


def test_is_market_open_false():
    assert is_market_open(_broker(market_open=False)) is False


def test_is_market_open_fails_closed_on_error():
    assert is_market_open(_broker(clock_error=True)) is False


# ---- run_once ----

def test_run_once_skips_when_market_closed(tmp_path):
    cfg = _config(ks_path=str(tmp_path / "ks.flag"))
    broker = _broker(market_open=False)
    repo = SQLiteRepository(str(tmp_path / "portfolio.db"))
    strategies = [_NeverCalledStrategy("AAPL")]

    results = run_once(cfg, strategies, broker, repo)
    assert results == []


def test_run_once_skips_when_kill_switch_engaged(tmp_path):
    ks_path = str(tmp_path / "kill.flag")
    cfg = _config(ks_path=ks_path)
    KillSwitch(ks_path).engage("test")

    broker = _broker(market_open=True)
    repo = SQLiteRepository(str(tmp_path / "portfolio.db"))
    strategies = [_NeverCalledStrategy("AAPL")]

    results = run_once(cfg, strategies, broker, repo)
    assert results == []


def test_run_once_calls_pipeline_when_open(tmp_path):
    import numpy as np
    import pandas as pd
    import trader.pipeline as _pipeline_mod

    ks_path = str(tmp_path / "kill.flag")  # file does not exist = disengaged
    cfg = _config(ks_path=ks_path)
    broker = _broker(market_open=True)
    repo = SQLiteRepository(str(tmp_path / "portfolio.db"))

    n = 120
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    close = pd.Series(100.0 + np.arange(n) * 0.5, index=dates)
    fake_bars = pd.DataFrame({
        "open": close, "high": close + 0.5, "low": close - 0.5,
        "close": close, "volume": 1_000_000,
    }, index=dates)

    from trader.strategy.base import Signal

    class _HoldStrategy(Strategy):
        def _decide(self, bars, asof) -> Signal:
            return Signal(self.symbol, "hold", 0.0, "test-hold")

    original_single = _pipeline_mod.get_daily_bars
    original_batch = _pipeline_mod.get_daily_bars_batch
    _pipeline_mod.get_daily_bars = lambda symbol, start, end, config=None: fake_bars
    _pipeline_mod.get_daily_bars_batch = lambda symbols, start, end, config=None: {s: fake_bars for s in symbols}
    try:
        results = run_once(cfg, [_HoldStrategy("AAPL")], broker, repo)
    finally:
        _pipeline_mod.get_daily_bars = original_single
        _pipeline_mod.get_daily_bars_batch = original_batch

    assert len(results) == 1
    assert results[0].outcome == "hold"


# ---- run_nightly_bandit_update ----
#
# AlpacaBroker.get_account_activities deliberately bypasses the injected client
# (raw HTTP — alpaca-py's SDK does not expose the endpoint consistently), so the
# test seam is the broker method itself, stubbed via a small counter wrapper.

def _stub_activities(broker: AlpacaBroker, fills: list[dict]) -> dict:
    calls = {"n": 0}

    def _fake(activity_type: str = "FILL", raise_on_error: bool = False):
        calls["n"] += 1
        return fills

    broker.get_account_activities = _fake  # type: ignore[method-assign]
    return calls


def _broker_with_fills(fills: list[dict], tmp_path) -> AlpacaBroker:
    cfg = _config(
        ks_path=str(tmp_path / "ks.flag"),
        db_path=str(tmp_path / "portfolio.db"),
    )
    broker = AlpacaBroker(
        cfg,
        client=FakeClockClient(),
        request_builder=lambda **kw: kw,
        order_filter_builder=lambda today: ("open", "closed"),
    )
    _stub_activities(broker, fills)
    return broker


def _seed_round_trips(repo, strategy, regime, symbol, n=20):
    """Seed n profitable round-trips so update_bandit_weights passes min_samples."""
    from trader.portfolio.repository import OrderRow
    fills = []
    for i in range(n):
        bid, sid = f"b{i}", f"s{i}"
        repo.record_order(OrderRow(
            client_order_id=f"c-b{i}", symbol=symbol, side="buy",
            notional=1000.0, status="accepted",
            broker_order_id=bid, strategy_name=strategy, regime=regime,
        ))
        repo.record_order(OrderRow(
            client_order_id=f"c-s{i}", symbol=symbol, side="sell",
            notional=1000.0, status="accepted",
            broker_order_id=sid, strategy_name=strategy, regime=regime,
        ))
        fills.append({"order_id": bid, "symbol": symbol, "side": "buy", "qty": 10, "price": 100.0})
        fills.append({"order_id": sid, "symbol": symbol, "side": "sell", "qty": 10, "price": 120.0})
    return fills


def test_nightly_bandit_skips_when_bandit_disabled(tmp_path):
    """Neither shadow nor live → function returns {} without calling broker."""
    from dataclasses import replace as _replace
    cfg = _config(ks_path=str(tmp_path / "ks.flag"), db_path=str(tmp_path / "portfolio.db"))
    # shadow defaults on as of 2026-07-18 — force both off to exercise the disabled path.
    cfg = _replace(cfg, risk=_replace(cfg.risk, bandit_weighting_shadow=False, bandit_weighting_live=False))
    broker = AlpacaBroker(
        cfg, client=FakeClockClient(),
        request_builder=lambda **kw: kw,
        order_filter_builder=lambda today: ("open", "closed"),
    )
    calls = _stub_activities(broker, [])
    repo = SQLiteRepository(str(tmp_path / "portfolio.db"))

    result = run_nightly_bandit_update(cfg, broker, repo, cycle_index=0)

    assert result == {}
    assert calls["n"] == 0


def test_nightly_bandit_shadow_calls_broker_and_updates_weights(tmp_path):
    """Shadow mode: broker fills fetched, weights computed and persisted."""
    from dataclasses import replace as _replace
    cfg = _config(ks_path=str(tmp_path / "ks.flag"), db_path=str(tmp_path / "portfolio.db"))
    cfg = _replace(cfg, risk=_replace(cfg.risk, bandit_weighting_shadow=True))

    repo = SQLiteRepository(str(tmp_path / "portfolio.db"))
    fills = _seed_round_trips(repo, "SuperTrend", "normal", "AAPL", n=20)

    broker = AlpacaBroker(
        cfg, client=FakeClockClient(),
        request_builder=lambda **kw: kw,
        order_filter_builder=lambda today: ("open", "closed"),
    )
    calls = _stub_activities(broker, fills)

    result = run_nightly_bandit_update(cfg, broker, repo, cycle_index=1)

    assert calls["n"] == 1
    assert ("SuperTrend", "normal") in result
    assert repo.get_bandit_weight("SuperTrend", "normal") > 1.0  # profitable fills raise weight


def test_nightly_bandit_empty_fills_returns_empty(tmp_path):
    """Broker returns no fills → no weights updated, returns {}."""
    from dataclasses import replace as _replace
    cfg = _config(ks_path=str(tmp_path / "ks.flag"), db_path=str(tmp_path / "portfolio.db"))
    cfg = _replace(cfg, risk=_replace(cfg.risk, bandit_weighting_live=True))

    repo = SQLiteRepository(str(tmp_path / "portfolio.db"))
    broker = _broker_with_fills([], tmp_path)

    result = run_nightly_bandit_update(cfg, broker, repo, cycle_index=0)

    assert result == {}
    assert repo.get_all_bandit_weights() == {}
