"""Entry-fraction (partial sizing) support in the backtest engine.

`entry_fraction` lets a sizing policy (e.g. vol targeting) decide what fraction
of capital a buy deploys; the remainder stays in cash. None preserves the
all-in behavior exactly.
"""
from __future__ import annotations

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


class _BuyThenSell(Strategy):
    """Buys on the first bar, sells on the fourth."""

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        if len(bars) == 1:
            return Signal(self.symbol, "buy", 1.0, "enter")
        if len(bars) == 4:
            return Signal(self.symbol, "sell", 1.0, "exit")
        return Signal(self.symbol, "hold", 0.0, "wait")


_RISING = [100.0, 100.0, 110.0, 120.0, 130.0, 140.0]


def test_none_matches_all_in_behavior():
    baseline = run_backtest(_bars(_RISING), _BuyOnce("X"), cost_model=_NO_COSTS)
    explicit = run_backtest(
        _bars(_RISING), _BuyOnce("X"), cost_model=_NO_COSTS, entry_fraction=None
    )
    pd.testing.assert_series_equal(baseline.equity_curve, explicit.equity_curve)


def test_full_fraction_matches_all_in_behavior():
    baseline = run_backtest(_bars(_RISING), _BuyOnce("X"), cost_model=_NO_COSTS)
    full = run_backtest(
        _bars(_RISING), _BuyOnce("X"), cost_model=_NO_COSTS,
        entry_fraction=lambda bars: 1.0,
    )
    pd.testing.assert_series_equal(baseline.equity_curve, full.equity_curve)


def test_half_fraction_deploys_half_and_keeps_cash():
    result = run_backtest(
        _bars(_RISING), _BuyOnce("X"), initial_cash=10_000.0,
        cost_model=_NO_COSTS, entry_fraction=lambda bars: 0.5,
    )
    # Entry at bar-1 open (100): 5,000 invested → 50 shares, 5,000 cash.
    # Final close 140 → equity = 5,000 + 50 * 140 = 12,000.
    assert float(result.equity_curve.iloc[-1]) == 12_000.0


def test_sell_exits_whole_position():
    result = run_backtest(
        _bars(_RISING), _BuyThenSell("X"), initial_cash=10_000.0,
        cost_model=_NO_COSTS, entry_fraction=lambda bars: 0.5,
    )
    # 50 shares in @100, all 50 out @130 (bar-4 decision fills at bar-5 open... )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.shares == 50.0
    # After the exit everything is cash: flat to the end.
    assert float(result.equity_curve.iloc[-1]) == float(
        result.equity_curve.loc[trade.exit_date]
    )


def test_reserved_cash_survives_round_trip():
    # Flat prices, no costs: a 0.5-fraction round trip must return ALL capital,
    # including the half that stayed in cash while the position was open.
    flat = [100.0] * 6
    result = run_backtest(
        _bars(flat), _BuyThenSell("X"), initial_cash=10_000.0,
        cost_model=_NO_COSTS, entry_fraction=lambda bars: 0.5,
    )
    assert len(result.trades) == 1
    assert float(result.equity_curve.iloc[-1]) == 10_000.0


def test_fraction_above_one_is_clamped_no_leverage():
    levered = run_backtest(
        _bars(_RISING), _BuyOnce("X"), cost_model=_NO_COSTS,
        entry_fraction=lambda bars: 2.0,
    )
    baseline = run_backtest(_bars(_RISING), _BuyOnce("X"), cost_model=_NO_COSTS)
    pd.testing.assert_series_equal(levered.equity_curve, baseline.equity_curve)


def test_nonpositive_fraction_falls_back_to_full_size():
    broken = run_backtest(
        _bars(_RISING), _BuyOnce("X"), cost_model=_NO_COSTS,
        entry_fraction=lambda bars: 0.0,
    )
    baseline = run_backtest(_bars(_RISING), _BuyOnce("X"), cost_model=_NO_COSTS)
    pd.testing.assert_series_equal(broken.equity_curve, baseline.equity_curve)


def test_entry_fraction_sees_only_visible_bars():
    seen: list[pd.Timestamp] = []

    def spy(bars: pd.DataFrame) -> float:
        seen.append(bars.index[-1])
        return 1.0

    bars = _bars(_RISING)
    run_backtest(bars, _BuyOnce("X"), cost_model=_NO_COSTS, entry_fraction=spy)
    # _BuyOnce buys at the first decision bar; the sizer must see exactly the
    # bars visible at that decision — ending at bar 0, before the fill bar.
    assert seen == [bars.index[0]]
