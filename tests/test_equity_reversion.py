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
    """Buy blocked by SMA200 filter even when close < lower_band and RSI oversold."""
    # Build a downtrend where current close is well below SMA200:
    # 220 bars: start at 200, fall steadily to 100.
    # At the end, close is well below SMA200 (which lags at ~150).
    # Add sharp drop at the end to push below lower_bb and RSI oversold.
    n = 220
    # Steady decline to 110
    base = list(np.linspace(200.0, 110.0, 210))
    # Sharp 10-bar drop to get close below lower_bb and RSI oversold
    drop_start = base[-1]
    drop = [drop_start - i * (drop_start * 0.008) for i in range(1, 11)]
    closes = base + drop
    bars = _make_bars(closes)
    sig = EquityBollingerReversion("X").generate(bars, bars.index[-1])
    # close is below lower_bb and RSI(2) is oversold, but close << SMA200 → blocked
    assert sig.side == "hold"


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


def test_sell_when_price_recovers_to_mid_band():
    """Exit triggered when close recovers above mid Bollinger Band."""
    # Build a series: uptrend for 210 bars then an alternating oscillation (±0.5/0.3)
    # for 20 bars around a level above the mid Bollinger Band.
    # Alternating gains/losses keep RSI(2) near 50 (well below the 85 RSI-exit threshold),
    # while the slightly rising oscillation keeps close above the mid-band.
    base = list(100.0 + np.arange(210) * 0.3)
    osc_start = base[-1]
    osc = []
    v = osc_start
    for i in range(20):
        v += 0.5 if i % 2 == 0 else -0.3
        osc.append(v)
    bars = _make_bars(base + osc)
    strat = EquityBollingerReversion("X")
    strat._in_position = True
    sig = strat.generate(bars, bars.index[-1])
    # close above mid-band, RSI(2) ~45 (not overbought) → mid-band exit (strength 0.8)
    assert sig.side == "sell"
    assert sig.strength == 0.8


# ---- empty data -------------------------------------------------------------

def test_empty_bars_returns_hold():
    bars = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    bars.index = pd.DatetimeIndex([])
    sig = EquityBollingerReversion("X").generate(bars, pd.Timestamp("2023-01-02"))
    assert sig.side == "hold"


def test_reset_state_clears_in_position():
    bars = _long_uptrend_then_dip(n=220)
    strat = EquityBollingerReversion("X")
    strat._in_position = True
    strat.reset_state()
    assert strat._in_position is False
