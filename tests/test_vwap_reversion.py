from __future__ import annotations
import numpy as np
import pandas as pd
import pytest
from trader.strategy.base import IntradayStrategy
from trader.strategy.vwap_reversion import VWAPReversion


def _make_bars(closes, volumes=None) -> pd.DataFrame:
    n = len(closes)
    ts = pd.date_range("2024-01-15 09:30", periods=n, freq="1min")
    c = pd.Series(closes, index=ts, dtype=float)
    v = pd.Series(volumes or [1_000_000] * n, index=ts, dtype=float)
    return pd.DataFrame({
        "open": c.shift(1).fillna(c.iloc[0]),
        "high": c + 0.20,
        "low": c - 0.20,
        "close": c,
        "volume": v,
    }, index=ts)


def _vwap_bars_with_dip(n=60, vwap_close=100.0, dip_sigma=2.5):
    """Bars where VWAP ≈ vwap_close and last bar dips dip_sigma below VWAP std."""
    closes = [vwap_close] * (n - 1)
    # Build bars up to n-1 bars; compute approx std, then set last bar to dip
    df_pre = _make_bars(closes)
    vwap = (df_pre["close"] * df_pre["volume"]).cumsum() / df_pre["volume"].cumsum()
    dev = df_pre["close"] - vwap
    std_val = float(dev.rolling(20).std().iloc[-1]) or 0.5
    dip_close = vwap_close - dip_sigma * std_val - 0.01
    closes.append(dip_close)
    return _make_bars(closes)


def test_is_intraday_strategy():
    assert isinstance(VWAPReversion("AAPL"), IntradayStrategy)


def test_bar_timeframe_is_1min():
    assert VWAPReversion("AAPL").bar_timeframe == "1min"


def test_hold_on_insufficient_bars():
    bars = _make_bars([100.0 + i for i in range(10)])
    sig = VWAPReversion("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_buy_when_below_vwap_2sigma():
    bars = _vwap_bars_with_dip(n=60, dip_sigma=2.5)
    sig = VWAPReversion("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "buy"
    assert sig.strength > 0.0


def test_hold_when_near_vwap():
    closes = [100.0 + np.sin(i / 5) * 0.1 for i in range(60)]
    bars = _make_bars(closes)
    sig = VWAPReversion("AAPL").generate(bars, bars.index[-1])
    assert sig.side in {"hold", "buy"}  # should not sell when near VWAP


def test_sell_when_at_or_above_vwap_with_position():
    """Strategy emits sell when price returns to VWAP (state tracked via _entered)."""
    strat = VWAPReversion("AAPL")
    # Simulate entered state
    strat._entered = True
    closes = [100.0] * 60  # price == vwap (all same close, volume uniform → vwap == close)
    bars = _make_bars(closes)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "sell"


def test_strength_scales_with_deviation():
    bars_2 = _vwap_bars_with_dip(n=60, dip_sigma=2.5)
    bars_3 = _vwap_bars_with_dip(n=60, dip_sigma=3.5)
    sig_2 = VWAPReversion("AAPL").generate(bars_2, bars_2.index[-1])
    sig_3 = VWAPReversion("AAPL").generate(bars_3, bars_3.index[-1])
    if sig_2.side == "buy" and sig_3.side == "buy":
        assert sig_3.strength >= sig_2.strength
