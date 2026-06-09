"""Tests for EquityBollingerReversion strategy."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader.strategy.base import Signal
from trader.strategy.equity_reversion import EquityBollingerReversion


def _make_bars(closes) -> pd.DataFrame:
    n = len(closes)
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    c = pd.Series(closes, index=dates, dtype=float)
    return pd.DataFrame({
        "open": c.shift(1).fillna(c.iloc[0]),
        "high": c + 0.5,
        "low": c - 0.5,
        "close": c,
        "volume": 1_000_000,
    }, index=dates)


def _long_uptrend_then_dip(n: int = 220) -> pd.DataFrame:
    """220-bar series: uptrend for 210 bars then a sharp 10-bar dip into oversold."""
    base = list(100.0 + np.arange(210) * 0.3)
    # Sharp dip: drop 8% over 10 bars
    dip_start = base[-1]
    dip = [dip_start - i * (dip_start * 0.008) for i in range(1, 11)]
    return _make_bars(base + dip)


# ---- contract ---------------------------------------------------------------

def test_returns_valid_signal():
    bars = _long_uptrend_then_dip()
    sig = EquityBollingerReversion("X").generate(bars, bars.index[-1])
    assert isinstance(sig, Signal)
    assert sig.side in {"buy", "sell", "hold"}
    assert 0.0 <= sig.strength <= 1.0


def test_insufficient_history_returns_hold():
    bars = _make_bars([100.0] * 10)
    sig = EquityBollingerReversion("X").generate(bars, bars.index[-1])
    assert sig.side == "hold"


# ---- mean reversion entry ---------------------------------------------------

def test_buy_on_oversold_dip_in_uptrend():
    bars = _long_uptrend_then_dip(n=220)
    sig = EquityBollingerReversion("X").generate(bars, bars.index[-1])
    # After a sharp dip in a long uptrend, should fire buy
    assert sig.side == "buy"
    assert sig.strength > 0.0


def test_no_buy_when_below_sma200():
    # Downtrend for 220 bars — close always well below SMA200 midpoint
    closes = list(200.0 - np.arange(220) * 0.5)
    bars = _make_bars(closes)
    sig = EquityBollingerReversion("X").generate(bars, bars.index[-1])
    # SMA200 filter should block buy in a downtrend
    assert sig.side in {"hold", "sell"}


# ---- exit logic -------------------------------------------------------------

def test_sell_when_rsi2_overbought():
    # Uptrend with very strong recent momentum → RSI(2) > 85
    n = 220
    closes = list(100.0 + np.arange(200) * 0.3)
    # Spike: last 20 bars rise steeply
    spike_start = closes[-1]
    closes += [spike_start + i * 2.5 for i in range(1, 21)]
    bars = _make_bars(closes)
    strat = EquityBollingerReversion("X")
    strat._in_position = True
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "sell"


# ---- empty data -------------------------------------------------------------

def test_empty_bars_returns_hold():
    bars = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    bars.index = pd.DatetimeIndex([])
    sig = EquityBollingerReversion("X").generate(bars, pd.Timestamp("2023-01-02"))
    assert sig.side == "hold"
