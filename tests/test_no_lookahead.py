"""KEYSTONE tests: the backtest cannot use information from the future.

Two independent guarantees:
  1. Strategy.generate never shows a subclass any bar later than `asof`.
  2. The engine fills at the NEXT bar's open, not the decision bar's close.
"""
from __future__ import annotations

import pandas as pd
import pytest

from trader.backtest.costs import CostModel
from trader.backtest.engine import run_backtest
from trader.strategy.base import Signal, Strategy


class _Spy(Strategy):
    """Records the latest bar index it was ever allowed to see."""

    max_seen: pd.Timestamp | None = None

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        self.max_seen = bars.index.max()
        return Signal(self.symbol, "hold", 0.0, "spy")


def test_strategy_cannot_see_past_asof(trending_bars):
    spy = _Spy("X")
    asof = trending_bars.index[50]
    spy.generate(trending_bars, asof)
    # Even though full history was passed, the subclass only saw bars <= asof.
    assert spy.max_seen == asof
    assert spy.max_seen <= asof


class _BuyOnceOn(Strategy):
    """Emits a single buy on a chosen decision date; holds otherwise."""

    def __init__(self, symbol: str, when: pd.Timestamp) -> None:
        super().__init__(symbol)
        self.when = pd.Timestamp(when)

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        if asof == self.when:
            return Signal(self.symbol, "buy", 1.0, "buy once")
        return Signal(self.symbol, "hold", 0.0, "hold")


def test_fill_uses_next_open_not_decision_close():
    dates = pd.date_range("2023-01-02", periods=5, freq="B")
    # Decision bar (index 1) has a spiking close of 150; the next bar opens at 200.
    bars = pd.DataFrame({
        "open":  [100.0, 101.0, 200.0, 103.0, 104.0],
        "high":  [101.0, 151.0, 201.0, 104.0, 105.0],
        "low":   [ 99.0, 100.0, 199.0, 102.0, 103.0],
        "close": [100.0, 150.0, 102.0, 103.0, 104.0],
        "volume": 1_000_000,
    }, index=dates)

    # Zero slippage/commission so the asserted price is exactly the reference open.
    result = run_backtest(
        bars,
        _BuyOnceOn("X", when=dates[1]),
        cost_model=CostModel(commission_per_trade=0.0, slippage_bps=0.0),
    )

    assert len(result.fills) == 1
    fill = result.fills[0]
    # Filled on the bar AFTER the decision...
    assert fill["date"] == dates[2]
    # ...at that bar's OPEN (200), never the decision bar's close (150).
    assert fill["price"] == pytest.approx(200.0)
    assert fill["price"] != pytest.approx(150.0)
