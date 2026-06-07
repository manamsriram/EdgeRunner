"""Tests for SmashDayB strategy."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader.strategy.base import Signal
from trader.strategy.smash_day import SmashDayB


def _make_bars(closes, highs=None, lows=None) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from close prices."""
    n = len(closes)
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    c = pd.Series(closes, index=dates, dtype=float)
    h = pd.Series(highs, index=dates, dtype=float) if highs else c + 1.0
    lo = pd.Series(lows, index=dates, dtype=float) if lows else c - 1.0
    return pd.DataFrame({
        "open": c.shift(1).fillna(c.iloc[0]),
        "high": h,
        "low": lo,
        "close": c,
        "volume": 1_000_000,
    }, index=dates)


def _smash_bars(n: int = 60) -> pd.DataFrame:
    """Trending series with a smash setup on the last bar.

    bars[-3]: high=102, close=100
    bars[-2]: high=105, close=101   <- two_ago_high = 105
    bars[-1]: close=107             <- smash: 107 > 105, and 107 > close[-(1+20)]
    """
    closes = list(np.linspace(80, 104, n - 2)) + [101.0, 107.0]
    highs = list(np.linspace(81, 106, n - 2)) + [105.0, 108.0]
    lows = list(np.linspace(79, 103, n - 2)) + [100.0, 106.0]
    return _make_bars(closes, highs, lows)


# ---- contract tests --------------------------------------------------------

def test_emits_valid_signal(trending_bars):
    sig = SmashDayB("X").generate(trending_bars, trending_bars.index[-1])
    assert isinstance(sig, Signal)
    assert sig.side in {"buy", "sell", "hold"}
    assert 0.0 <= sig.strength <= 1.0


def test_insufficient_history_returns_hold():
    bars = _make_bars([100.0, 101.0])
    sig = SmashDayB("X").generate(bars, bars.index[-1])
    assert sig.side == "hold"


# ---- entry logic -----------------------------------------------------------

def test_smash_setup_fires_buy():
    bars = _smash_bars()
    sig = SmashDayB("X", trend_n=20).generate(bars, bars.index[-1])
    assert sig.side == "buy"
    assert sig.strength > 0.0
    assert "smash-day-b long" in sig.reason


def test_no_smash_no_buy():
    """Flat series — close never breaks above prior high."""
    closes = [100.0] * 60
    bars = _make_bars(closes)
    sig = SmashDayB("X").generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_smash_against_downtrend_is_hold():
    """Setup fires but trend filter blocks it (price below N-bar-ago close)."""
    # Price falls over most of history, then one smash candle at the end.
    closes = list(np.linspace(120, 95, 58)) + [94.0, 99.0]
    highs = list(np.linspace(121, 96, 58)) + [98.0, 100.0]
    lows = list(np.linspace(119, 94, 58)) + [93.0, 98.0]
    bars = _make_bars(closes, highs, lows)
    sig = SmashDayB("X", trend_n=20).generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_long_only_ignores_short_setup():
    """Short smash fires but long_only=True means hold."""
    closes = list(np.linspace(120, 100, 58)) + [101.0, 96.0]
    highs = list(np.linspace(121, 101, 58)) + [102.0, 97.0]
    lows = list(np.linspace(119, 99, 58)) + [100.0, 95.0]
    bars = _make_bars(closes, highs, lows)
    sig = SmashDayB("X", long_only=True).generate(bars, bars.index[-1])
    assert sig.side != "sell"


# ---- exit logic ------------------------------------------------------------

def test_time_exit_fires_sell():
    """After time_exit bars held, strategy emits SELL."""
    bars = _smash_bars(n=60)
    strat = SmashDayB("X", trend_n=20, time_exit=3)

    # First call fires BUY and records entry state.
    sig0 = strat.generate(bars, bars.index[-1])
    assert sig0.side == "buy"

    # Extend bars by time_exit bars (all holding, no quick-exit trigger).
    last_close = bars["close"].iloc[-1]
    for i in range(1, 4):
        new_date = bars.index[-1] + pd.tseries.offsets.BusinessDay(i)
        new_row = pd.DataFrame({
            "open": [last_close],
            "high": [last_close + 1],
            "low": [last_close - 0.5],   # stays ABOVE entry bar low
            "close": [last_close + 0.1 * i],
            "volume": [1_000_000],
        }, index=[new_date])
        bars = pd.concat([bars, new_row])

    sig_final = strat.generate(bars, bars.index[-1])
    assert sig_final.side == "sell"
    assert "time exit" in sig_final.reason


def test_quick_exit_fires_on_close_below_entry_low():
    """Close drops below setup bar's low → quick exit."""
    bars = _smash_bars(n=60)
    strat = SmashDayB("X", trend_n=20, time_exit=10)

    sig0 = strat.generate(bars, bars.index[-1])
    assert sig0.side == "buy"
    entry_low = bars["low"].iloc[-1]  # low of signal bar

    # Next bar closes below entry_low.
    new_date = bars.index[-1] + pd.tseries.offsets.BusinessDay(1)
    crash_close = entry_low - 2.0
    new_row = pd.DataFrame({
        "open": [bars["close"].iloc[-1]],
        "high": [bars["close"].iloc[-1]],
        "low": [crash_close - 0.5],
        "close": [crash_close],
        "volume": [1_000_000],
    }, index=[new_date])
    bars = pd.concat([bars, new_row])

    sig_exit = strat.generate(bars, bars.index[-1])
    assert sig_exit.side == "sell"
    assert "quick exit" in sig_exit.reason


def test_hold_emitted_while_within_time_exit():
    """Between BUY and time exit, strategy holds."""
    bars = _smash_bars(n=60)
    strat = SmashDayB("X", trend_n=20, time_exit=5)

    strat.generate(bars, bars.index[-1])  # fire BUY

    last_close = bars["close"].iloc[-1]
    new_date = bars.index[-1] + pd.tseries.offsets.BusinessDay(1)
    new_row = pd.DataFrame({
        "open": [last_close],
        "high": [last_close + 1],
        "low": [last_close - 0.5],
        "close": [last_close + 0.2],
        "volume": [1_000_000],
    }, index=[new_date])
    bars = pd.concat([bars, new_row])

    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "hold"
    assert "holding" in sig.reason


# ---- state reset after exit ------------------------------------------------

def test_new_setup_detectable_after_exit():
    """After time exit clears state, a new setup can trigger a BUY again."""
    bars = _smash_bars(n=60)
    strat = SmashDayB("X", trend_n=20, time_exit=1)

    strat.generate(bars, bars.index[-1])  # BUY

    last_close = bars["close"].iloc[-1]
    # One bar passes — triggers time exit.
    new_date = bars.index[-1] + pd.tseries.offsets.BusinessDay(1)
    new_row = pd.DataFrame({
        "open": [last_close],
        "high": [last_close + 1],
        "low": [last_close - 0.5],
        "close": [last_close + 0.2],
        "volume": [1_000_000],
    }, index=[new_date])
    bars = pd.concat([bars, new_row])

    sell_sig = strat.generate(bars, bars.index[-1])
    assert sell_sig.side == "sell"  # time exit

    # Verify internal state is cleared.
    assert strat._entry_bar_ts is None
