from __future__ import annotations
import pandas as pd
import pytest
from trader.strategy.base import IntradayStrategy
from trader.strategy.intraday_trend import IntradayTrend


def _make_intraday_bars(closes, n_per_day=80) -> pd.DataFrame:
    """Minute-level OHLCV bars — mimics intraday bar format."""
    n = len(closes)
    timestamps = pd.date_range("2024-01-15 09:30", periods=n, freq="5min")
    c = pd.Series(closes, index=timestamps, dtype=float)
    return pd.DataFrame({
        "open": c.shift(1).fillna(c.iloc[0]),
        "high": c + 0.5,
        "low": c - 0.5,
        "close": c,
        "volume": 500_000,
    }, index=timestamps)


def _uptrend(n=80):
    return _make_intraday_bars([100.0 + i * 0.6 for i in range(n)])


def _downtrend(n=80):
    return _make_intraday_bars([150.0 - i * 0.6 for i in range(n)])


def test_is_intraday_strategy():
    assert isinstance(IntradayTrend("AAPL"), IntradayStrategy)


def test_bar_timeframe_is_5min():
    assert IntradayTrend("AAPL").bar_timeframe == "5min"


def test_buy_in_uptrend():
    bars = _uptrend()
    sig = IntradayTrend("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "buy"
    assert 0.0 < sig.strength <= 1.0


def test_sell_in_downtrend():
    bars = _downtrend()
    sig = IntradayTrend("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "sell"


def test_hold_on_insufficient_history():
    bars = _make_intraday_bars([100.0 + i for i in range(5)])
    sig = IntradayTrend("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_hold_in_choppy_market():
    n = 80
    closes = [100.0 + (0.05 if i % 2 == 0 else -0.05) for i in range(n)]
    bars = _make_intraday_bars(closes)
    sig = IntradayTrend("AAPL", adx_threshold=20.0).generate(bars, bars.index[-1])
    assert sig.side == "hold"
