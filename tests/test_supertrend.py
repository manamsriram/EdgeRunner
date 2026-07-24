"""Tests for SuperTrend strategy."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader.strategy.base import Signal
from trader.strategy.supertrend import SuperTrend


def _make_bars(closes, highs=None, lows=None) -> pd.DataFrame:
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


def _uptrend_bars(n: int = 80) -> pd.DataFrame:
    closes = list(100.0 + np.arange(n) * 0.6)
    return _make_bars(closes)


def _downtrend_bars(n: int = 80) -> pd.DataFrame:
    closes = list(150.0 - np.arange(n) * 0.6)
    return _make_bars(closes)


# ---- contract ---------------------------------------------------------------

def test_returns_valid_signal(trending_bars):
    sig = SuperTrend("X").generate(trending_bars, trending_bars.index[-1])
    assert isinstance(sig, Signal)
    assert sig.side in {"buy", "sell", "hold"}
    assert 0.0 <= sig.strength <= 1.0


def test_insufficient_history_returns_hold():
    bars = _make_bars([100.0, 101.0, 102.0])
    sig = SuperTrend("X").generate(bars, bars.index[-1])
    assert sig.side == "hold"


# ---- uptrend ----------------------------------------------------------------

def test_buy_signal_in_uptrend_with_strong_adx():
    bars = _uptrend_bars(n=80)
    sig = SuperTrend("X", atr_n=14, multiplier=3.0, adx_threshold=20.0).generate(
        bars, bars.index[-1]
    )
    assert sig.side == "buy"
    assert sig.strength > 0.0


# ---- ADX filter -------------------------------------------------------------

def test_hold_in_uptrend_when_adx_below_threshold():
    # Flat market: close oscillates ±0.05 around 100 — no trend, low ADX.
    n = 80
    closes = [100.0 + (0.05 if i % 2 == 0 else -0.05) for i in range(n)]
    bars = _make_bars(closes)
    sig = SuperTrend("X", adx_threshold=20.0).generate(bars, bars.index[-1])
    # Low ADX regime: must not generate buy
    assert sig.side == "hold"


# ---- downtrend --------------------------------------------------------------

def test_sell_signal_in_downtrend():
    bars = _downtrend_bars(n=80)
    sig = SuperTrend("X", atr_n=14, multiplier=3.0, adx_threshold=20.0).generate(
        bars, bars.index[-1]
    )
    assert sig.side == "sell"


# ---- ADX-rising filter -------------------------------------------------------

def test_hold_when_adx_falling_even_above_threshold():
    # Sharp initial trend (ADX shoots up) followed by a choppy near-flat
    # continuation (ADX decays back down) while still technically an uptrend —
    # a late entry into an already-exhausting trend must be rejected.
    n = 80
    closes = list(100.0 + np.arange(30) * 3.0)  # steep run-up, ADX climbs hard
    last = closes[-1]
    for i in range(n - 30):
        last += 0.3 if i % 2 == 0 else -0.25  # choppy, barely-positive drift
        closes.append(last)
    bars = _make_bars(closes)
    sig = SuperTrend("X", atr_n=14, multiplier=3.0, adx_threshold=20.0).generate(
        bars, bars.index[-1]
    )
    assert sig.side == "hold"
    assert "falling" in sig.reason


# ---- re-entry cooldown --------------------------------------------------------

def test_cooldown_blocks_immediate_reentry_after_exit():
    bars = _uptrend_bars(n=80)
    strat = SuperTrend("X", atr_n=14, multiplier=3.0, adx_threshold=20.0, reentry_cooldown_bars=3)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "buy"

    strat.reset_state()  # pipeline calls this on every confirmed exit
    sig2 = strat.generate(bars, bars.index[-1])
    assert sig2.side == "hold"
    assert "cooldown" in sig2.reason


def test_cooldown_expires_after_n_bars():
    bars = _uptrend_bars(n=80)
    strat = SuperTrend("X", atr_n=14, multiplier=3.0, adx_threshold=20.0, reentry_cooldown_bars=2)
    strat.generate(bars, bars.index[-1])
    strat.reset_state()

    for _ in range(2):
        sig = strat.generate(bars, bars.index[-1])
        assert sig.side == "hold"

    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "buy"


# ---- empty data -------------------------------------------------------------

def test_empty_bars_returns_hold():
    bars = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    bars.index = pd.DatetimeIndex([])
    sig = SuperTrend("X").generate(bars, pd.Timestamp("2023-01-02"))
    assert sig.side == "hold"
