from __future__ import annotations
import pandas as pd
import pytest
from trader.strategy.base import IntradayStrategy
from trader.strategy.orb import OpeningRangeBreakout

_RANGE_BARS = 30


def _make_bars(closes, highs=None, lows=None, volumes=None) -> pd.DataFrame:
    n = len(closes)
    ts = pd.date_range("2024-01-15 09:30", periods=n, freq="1min")
    c = pd.Series(closes, index=ts, dtype=float)
    h = pd.Series(highs, index=ts, dtype=float) if highs else c + 0.5
    lo = pd.Series(lows, index=ts, dtype=float) if lows else c - 0.5
    v = pd.Series(volumes or [1_000_000] * n, index=ts, dtype=float)
    return pd.DataFrame({"open": c, "high": h, "low": lo, "close": c, "volume": v}, index=ts)


def _range_bars_with_breakout(range_high=105.0, range_low=95.0, breakout_close=106.0):
    """30 range bars + 1 breakout bar with high volume."""
    closes = [100.0] * _RANGE_BARS + [breakout_close]  # 30 + 1 = 31 bars
    highs = [range_high] * _RANGE_BARS + [breakout_close + 0.5]
    lows = [range_low] * _RANGE_BARS + [breakout_close - 0.5]
    volumes = [500_000] * _RANGE_BARS + [1_500_000]  # last bar: high vol
    return _make_bars(closes, highs, lows, volumes)


def test_is_intraday_strategy():
    assert isinstance(OpeningRangeBreakout("AAPL"), IntradayStrategy)


def test_bar_timeframe_is_1min():
    assert OpeningRangeBreakout("AAPL").bar_timeframe == "1min"


def test_hold_before_range_set():
    bars = _make_bars([100.0] * 10)
    sig = OpeningRangeBreakout("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_buy_on_breakout_above_orh():
    bars = _range_bars_with_breakout(range_high=105.0, breakout_close=106.0)
    sig = OpeningRangeBreakout("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "buy"
    assert sig.strength > 0.0


def test_no_entry_inside_range():
    """Close inside range after range is set → hold."""
    closes = [100.0] * _RANGE_BARS + [102.0]  # 102 < range_high of 100.5 is inside
    highs = [100.5] * _RANGE_BARS + [102.5]
    lows = [99.5] * _RANGE_BARS + [101.5]
    # ORH = max(high[0:30]) = 100.5; close 102 > 100.5 → actually a breakout
    # Let's keep close below ORH
    closes = [100.0] * _RANGE_BARS + [100.3]
    highs = [100.5] * _RANGE_BARS + [100.8]
    lows = [99.5] * _RANGE_BARS + [100.0]
    bars = _make_bars(closes, highs, lows)
    sig = OpeningRangeBreakout("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_sell_when_close_drops_below_orl():
    strat = OpeningRangeBreakout("AAPL")
    strat._range_set = True
    strat._orh = 105.0
    strat._orl = 95.0
    strat._entered = True
    closes = [100.0] * _RANGE_BARS + [94.0]  # below ORL
    bars = _make_bars(closes)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "sell"


def test_no_reentry_after_exit():
    """After exiting, no new buy even on another breakout."""
    strat = OpeningRangeBreakout("AAPL")
    strat._range_set = True
    strat._orh = 105.0
    strat._orl = 95.0
    strat._entered = False
    strat._exited = True
    bars = _range_bars_with_breakout(range_high=105.0, breakout_close=106.0)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "hold"
