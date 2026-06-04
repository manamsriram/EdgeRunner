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
from trader.scheduler import is_market_open, run_once
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

    original = _pipeline_mod.get_daily_bars
    _pipeline_mod.get_daily_bars = lambda symbol, start, end, config=None: fake_bars
    try:
        results = run_once(cfg, [_HoldStrategy("AAPL")], broker, repo)
    finally:
        _pipeline_mod.get_daily_bars = original

    assert len(results) == 1
    assert results[0].outcome == "hold"
