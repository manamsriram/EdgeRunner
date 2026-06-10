"""Stop-loss modeling in the backtest engine.

Mirrors the live pipeline: when the decision bar's close is `stop_loss_pct` or more
below the entry fill price, the position is force-sold at the next bar's open,
overriding the strategy's signal. The strategy may re-enter afterwards (the live
cascade-re-entry behavior this exists to measure).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from trader.backtest.costs import CostModel
from trader.backtest.engine import run_backtest
from trader.strategy.base import Signal, Strategy

_NO_COSTS = CostModel(commission_per_trade=0.0, slippage_bps=0.0)


def _bars(values: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2023-01-02", periods=len(values), freq="B")
    close = pd.Series(values, index=dates, dtype=float)
    return pd.DataFrame({
        "open": close, "high": close + 0.5, "low": close - 0.5,
        "close": close, "volume": 1_000_000,
    }, index=dates)


class _BuyOnce(Strategy):
    """Buys on the first decision bar, holds forever after."""

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        side = "buy" if len(bars) == 1 else "hold"
        return Signal(self.symbol, side, 1.0, f"fixed-{side}")


class _AlwaysBuy(Strategy):
    """Signals buy every bar — engine re-enters whenever flat."""

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        return Signal(self.symbol, "buy", 1.0, "fixed-buy")


# Entry fills at bar 1 open (100). Bar 5 close 91 is -9% from entry → stop.
_DECLINE = [100, 100, 100, 100, 100, 91, 90, 89, 80, 79, 78, 77]


def test_stop_loss_force_sells_at_next_open():
    result = run_backtest(
        _bars(_DECLINE), _BuyOnce("X"), cost_model=_NO_COSTS, stop_loss_pct=0.08
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == 100.0
    assert trade.exit_price == 90.0          # next open after the -9% close
    sell_fills = [f for f in result.fills if f["side"] == "sell"]
    assert len(sell_fills) == 1
    assert "stop-loss" in sell_fills[0]["reason"]


def test_no_stop_loss_by_default():
    result = run_backtest(_bars(_DECLINE), _BuyOnce("X"), cost_model=_NO_COSTS)
    assert result.trades == []               # held to the end, never sold


def test_stop_loss_overrides_buy_and_allows_reentry():
    """AlwaysBuy keeps signalling buy through the decline: stop must override the
    buy on the trigger bar, and the strategy re-enters on the following bar —
    the live cascade-re-entry pattern."""
    result = run_backtest(
        _bars(_DECLINE), _AlwaysBuy("X"), cost_model=_NO_COSTS, stop_loss_pct=0.08
    )
    assert len(result.trades) == 2
    first, second = result.trades
    assert first.exit_price == 90.0          # stopped out of entry @100
    assert second.entry_price == 89.0        # re-entered next bar
    assert second.exit_price == 79.0         # stopped again (80 close vs 89 entry)
    assert all(t.return_pct < -0.08 for t in result.trades)
