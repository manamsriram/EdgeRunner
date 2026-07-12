"""Stop-loss modeling in the backtest engine.

Mirrors a live resting broker stop: the position exits the moment a bar trades at or
below the stop level (bar low <= entry*(1-stop_loss_pct)), filling at the stop price —
or at the open when the bar gaps down through it. The strategy may re-enter afterwards
(the live cascade-re-entry behavior this exists to measure).
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


# Entry fills at bar 1 open (100), stop level = 92. Bar 5 opens at 91, gapping down
# through the stop, so it fills at the open (91). Bars are open==close, low = close-0.5.
_DECLINE = [100, 100, 100, 100, 100, 91, 90, 89, 80, 79, 78, 77]


def test_stop_loss_fills_at_gap_open_through_stop():
    result = run_backtest(
        _bars(_DECLINE), _BuyOnce("X"), cost_model=_NO_COSTS, stop_loss_pct=0.08
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == 100.0
    # Bar 5 gaps open to 91 (below the 92 stop) → fill at the open, not the stop.
    assert trade.exit_price == 91.0
    sell_fills = [f for f in result.fills if f["side"] == "sell"]
    assert len(sell_fills) == 1
    assert "stop-loss" in sell_fills[0]["reason"]


def test_stop_fills_at_stop_level_when_no_gap():
    # Entry @100, stop level 92. Bar 6 opens 92.5 (above the stop) but its low 92.0
    # touches it intrabar → the resting stop fills at the stop level, not the open.
    bars = _bars([100, 100, 100, 100, 100, 93, 92.5, 90, 89, 88, 87, 86])
    result = run_backtest(bars, _BuyOnce("X"), cost_model=_NO_COSTS, stop_loss_pct=0.08)
    assert len(result.trades) == 1
    assert result.trades[0].exit_price == 92.0


def test_no_stop_loss_by_default():
    result = run_backtest(_bars(_DECLINE), _BuyOnce("X"), cost_model=_NO_COSTS)
    assert result.trades == []               # held to the end, never sold


def test_stop_loss_overrides_buy_and_allows_reentry():
    """AlwaysBuy keeps signalling buy through the decline: the stop exit takes
    priority over the buy on the trigger bar, and the strategy re-enters afterwards —
    the live cascade-re-entry pattern."""
    result = run_backtest(
        _bars(_DECLINE), _AlwaysBuy("X"), cost_model=_NO_COSTS, stop_loss_pct=0.08
    )
    assert len(result.trades) == 2
    first, second = result.trades
    assert first.entry_price == 100.0
    assert first.exit_price == 91.0          # gap-through stop out of entry @100
    assert second.entry_price == 90.0        # re-entered at the next open
    assert second.exit_price == 80.0         # stopped again (bar 8 opens 80, stop 82.8)
    assert all(t.return_pct < -0.08 for t in result.trades)
