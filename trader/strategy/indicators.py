"""Technical indicators, implemented natively in pandas.

Implemented here rather than via pandas-ta: the current pandas-ta release requires
Python >=3.12 and depends on the removed numpy.NaN symbol, which breaks on this stack.
These three indicators (SMA, RSI, momentum) cover the shipped strategies.

Every function returns a Series aligned to the input index. Because each value at
position i depends only on data at positions <= i, slicing the input at `asof` before
calling these (as Strategy.generate does) is sufficient to guarantee no lookahead.
"""
from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=window, min_periods=window).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index in [0, 100]."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing == EMA with alpha = 1/window.
    avg_gain = gain.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    # When there are no losses, RS is +inf -> RSI 100; encode that explicitly.
    out = out.where(avg_loss != 0.0, 100.0)
    # If there are neither gains nor losses (flat series), RSI is neutral.
    out = out.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    return out


def momentum(series: pd.Series, window: int) -> pd.Series:
    """Fractional return over `window` bars: price_t / price_{t-window} - 1."""
    return series.pct_change(periods=window)
