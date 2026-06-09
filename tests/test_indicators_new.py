"""Tests for new indicators: adx, supertrend."""
from __future__ import annotations

import numpy as np
import pandas as pd

from trader.strategy.indicators import adx, supertrend


def _trending_bars(n: int = 100, rising: bool = True):
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    close = pd.Series(
        100.0 + np.arange(n) * (0.5 if rising else -0.5), index=dates
    )
    high = close + 0.5
    low = close - 0.5
    return high, low, close


# ---- adx -------------------------------------------------------------------

def test_adx_returns_series_same_length():
    high, low, close = _trending_bars()
    result = adx(high, low, close, window=14)
    assert isinstance(result, pd.Series)
    assert len(result) == len(close)


def test_adx_nan_before_warmup():
    high, low, close = _trending_bars(n=100)
    result = adx(high, low, close, window=14)
    # First 26 bars are NaN (warmup period: ~2*window - 2)
    assert result.iloc[:26].isna().all()


def test_adx_values_in_range():
    high, low, close = _trending_bars()
    result = adx(high, low, close, window=14)
    valid = result.dropna()
    assert (valid >= 0.0).all()
    assert (valid <= 100.0).all()


def test_adx_high_on_strong_trend():
    # 100-bar strong uptrend should produce ADX > 20 near the end
    high, low, close = _trending_bars(n=100)
    result = adx(high, low, close, window=14)
    assert result.iloc[-1] > 20.0


def test_adx_low_on_choppy_market():
    # Alternating +0.1 / -0.1 moves — no trend
    n = 100
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    close = pd.Series(
        [100.0 + (0.1 if i % 2 == 0 else -0.1) * (i % 10) for i in range(n)],
        index=dates,
    )
    high = close + 0.3
    low = close - 0.3
    result = adx(high, low, close, window=14)
    assert result.dropna().iloc[-1] < 25.0


def test_adx_no_lookahead():
    """Verify ADX has no lookahead: truncating input doesn't change past values."""
    high, low, close = _trending_bars(n=100)
    asof = close.index[59]
    full = adx(high, low, close, window=14)
    trunc = adx(high.loc[:asof], low.loc[:asof], close.loc[:asof], window=14)
    pd.testing.assert_series_equal(full.loc[:asof], trunc)


# ---- supertrend ------------------------------------------------------------

def test_supertrend_returns_two_series():
    high, low, close = _trending_bars()
    st_line, direction = supertrend(high, low, close)
    assert isinstance(st_line, pd.Series)
    assert isinstance(direction, pd.Series)
    assert len(st_line) == len(close)
    assert len(direction) == len(close)


def test_supertrend_direction_in_uptrend():
    high, low, close = _trending_bars(n=60, rising=True)
    _, direction = supertrend(high, low, close, atr_n=14, multiplier=3.0)
    # Last bar of a clean uptrend should be +1
    assert direction.dropna().iloc[-1] == 1.0


def test_supertrend_direction_in_downtrend():
    high, low, close = _trending_bars(n=60, rising=False)
    _, direction = supertrend(high, low, close, atr_n=14, multiplier=3.0)
    assert direction.dropna().iloc[-1] == -1.0


def test_supertrend_nan_before_warmup():
    high, low, close = _trending_bars(n=60)
    st_line, _ = supertrend(high, low, close, atr_n=14)
    assert st_line.iloc[:13].isna().all()


def test_supertrend_line_below_close_in_uptrend():
    high, low, close = _trending_bars(n=60, rising=True)
    st_line, direction = supertrend(high, low, close, atr_n=14, multiplier=3.0)
    last_valid = st_line.dropna()
    last_dir = direction.dropna()
    # In uptrend, supertrend line is the lower support band (below close)
    last_idx = last_valid.index[-1]
    if last_dir.loc[last_idx] == 1.0:
        assert float(last_valid.iloc[-1]) < float(close.loc[last_idx])


def test_supertrend_no_lookahead():
    """Verify SuperTrend has no lookahead: truncating input doesn't change past values."""
    high, low, close = _trending_bars(n=100)
    asof = close.index[59]
    st_full, dir_full = supertrend(high, low, close)
    st_trunc, dir_trunc = supertrend(high.loc[:asof], low.loc[:asof], close.loc[:asof])
    pd.testing.assert_series_equal(st_full.loc[:asof], st_trunc)
    pd.testing.assert_series_equal(dir_full.loc[:asof], dir_trunc)
