"""Tests for GapPatternA strategy."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader.strategy.base import Signal
from trader.strategy.gap_pattern import GapPatternA


def _make_bars(closes, highs=None, lows=None, opens=None) -> pd.DataFrame:
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


def _gap_up_bars(n: int = 40) -> pd.DataFrame:
    """Uptrending series with a gap-up on the last bar.

    bars[-2]: high=101, low=99, close=100   <- two_ago_high = 101
    bars[-1]: low=103 > 101 (gap up), close=106 > 20-bar channel
    """
    closes = list(np.linspace(80, 100, n - 2)) + [100.0, 106.0]
    highs  = list(np.linspace(81, 101, n - 2)) + [101.0, 107.0]
    lows   = list(np.linspace(79,  99, n - 2)) + [ 99.0, 103.0]
    return _make_bars(closes, highs, lows)


# ---- contract tests --------------------------------------------------------

def test_emits_valid_signal():
    bars = _gap_up_bars()
    sig = GapPatternA("X").generate(bars, bars.index[-1])
    assert isinstance(sig, Signal)
    assert sig.side in {"buy", "sell", "hold"}
    assert 0.0 <= sig.strength <= 1.0


def test_insufficient_history_returns_hold():
    bars = _make_bars([100.0] * 5)
    sig = GapPatternA("X", filter_n=20).generate(bars, bars.index[-1])
    assert sig.side == "hold"


# ---- entry logic -----------------------------------------------------------

def test_gap_up_with_trend_fires_buy():
    bars = _gap_up_bars()
    sig = GapPatternA("X", filter_n=20).generate(bars, bars.index[-1])
    assert sig.side == "buy"
    assert sig.strength > 0.0
    assert "gap-a long" in sig.reason


def test_gap_up_without_trend_is_hold():
    """Gap fires but close is below N-bar channel — trend filter blocks it."""
    # Downtrending series with a gap-up at the end that doesn't clear the channel.
    closes = list(np.linspace(120, 100, 38)) + [100.0, 102.0]
    highs  = list(np.linspace(121, 101, 38)) + [101.0, 102.5]
    lows   = list(np.linspace(119,  99, 38)) + [ 99.0, 101.5]
    bars = _make_bars(closes, highs, lows)
    sig = GapPatternA("X", filter_n=20).generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_no_gap_no_buy():
    """Low of last bar does not exceed prior high — no gap."""
    closes = list(np.linspace(80, 106, 40))
    highs  = [c + 1.0 for c in closes]
    lows   = [c - 1.0 for c in closes]
    bars = _make_bars(closes, highs, lows)
    sig = GapPatternA("X", filter_n=20).generate(bars, bars.index[-1])
    assert sig.side != "buy"


def test_long_only_ignores_short_gap():
    """Gap down fires but long_only=True → hold."""
    closes = list(np.linspace(120, 80, 38)) + [80.0, 74.0]
    highs  = list(np.linspace(121, 81, 38)) + [81.0, 75.0]
    lows   = list(np.linspace(119, 79, 38)) + [79.0, 73.0]
    bars = _make_bars(closes, highs, lows)
    sig = GapPatternA("X", long_only=True).generate(bars, bars.index[-1])
    assert sig.side != "sell"


# ---- exit logic ------------------------------------------------------------

def test_pattern_exit_fires_when_gap_fills():
    """Close below pre-gap high → pattern exit sell."""
    bars = _gap_up_bars()
    strat = GapPatternA("X", filter_n=20, time_exit=10)

    sig0 = strat.generate(bars, bars.index[-1])
    assert sig0.side == "buy"
    gap_ref = float(bars["high"].iloc[-2])  # two_ago_high = gap reference

    # Next bar closes below gap reference level.
    new_date = bars.index[-1] + pd.tseries.offsets.BusinessDay(1)
    crash_close = gap_ref - 2.0
    new_row = pd.DataFrame({
        "open":   [bars["close"].iloc[-1]],
        "high":   [bars["close"].iloc[-1]],
        "low":    [crash_close - 0.5],
        "close":  [crash_close],
        "volume": [1_000_000],
    }, index=[new_date])
    bars = pd.concat([bars, new_row])

    sig_exit = strat.generate(bars, bars.index[-1])
    assert sig_exit.side == "sell"
    assert "pattern exit" in sig_exit.reason


def test_time_exit_fires_after_n_bars():
    bars = _gap_up_bars()
    strat = GapPatternA("X", filter_n=20, time_exit=3)

    sig0 = strat.generate(bars, bars.index[-1])
    assert sig0.side == "buy"

    last_close = float(bars["close"].iloc[-1])
    for i in range(1, 4):
        new_date = bars.index[-1] + pd.tseries.offsets.BusinessDay(i)
        new_row = pd.DataFrame({
            "open":   [last_close],
            "high":   [last_close + 1],
            "low":    [last_close + 0.5],   # stays well above gap_ref (~101)
            "close":  [last_close + 0.2 * i],
            "volume": [1_000_000],
        }, index=[new_date])
        bars = pd.concat([bars, new_row])

    sig_final = strat.generate(bars, bars.index[-1])
    assert sig_final.side == "sell"
    assert "time exit" in sig_final.reason


def test_hold_emitted_while_within_time_exit():
    bars = _gap_up_bars()
    strat = GapPatternA("X", filter_n=20, time_exit=5)

    strat.generate(bars, bars.index[-1])  # fire BUY

    last_close = float(bars["close"].iloc[-1])
    new_date = bars.index[-1] + pd.tseries.offsets.BusinessDay(1)
    new_row = pd.DataFrame({
        "open":   [last_close],
        "high":   [last_close + 1],
        "low":    [last_close + 0.5],
        "close":  [last_close + 0.3],
        "volume": [1_000_000],
    }, index=[new_date])
    bars = pd.concat([bars, new_row])

    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "hold"
    assert "holding" in sig.reason


def test_state_reset_after_exit():
    """After time exit clears state, _entry_bar_ts is None."""
    bars = _gap_up_bars()
    strat = GapPatternA("X", filter_n=20, time_exit=1)

    strat.generate(bars, bars.index[-1])  # BUY

    last_close = float(bars["close"].iloc[-1])
    new_date = bars.index[-1] + pd.tseries.offsets.BusinessDay(1)
    new_row = pd.DataFrame({
        "open":   [last_close],
        "high":   [last_close + 1],
        "low":    [last_close + 0.5],
        "close":  [last_close + 0.2],
        "volume": [1_000_000],
    }, index=[new_date])
    bars = pd.concat([bars, new_row])

    sell_sig = strat.generate(bars, bars.index[-1])
    assert sell_sig.side == "sell"
    assert strat._entry_bar_ts is None
    assert strat._gap_ref_level is None
