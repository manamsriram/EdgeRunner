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


def ema(series: pd.Series, window: int) -> pd.Series:
    """Exponential moving average (span=window)."""
    return series.ewm(span=window, min_periods=window, adjust=False).mean()


def bollinger_bands(
    series: pd.Series, window: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper, mid, lower) Bollinger Bands."""
    mid = sma(series, window)
    std = series.rolling(window=window, min_periods=window).std()
    return mid + num_std * std, mid, mid - num_std * std


def zscore(series: pd.Series, window: int) -> pd.Series:
    """Rolling z-score: (x - mean) / std. NaN when std is zero."""
    mean = series.rolling(window=window, min_periods=window).mean()
    std = series.rolling(window=window, min_periods=window).std()
    return (series - mean) / std.replace(0.0, float("nan"))


def rolling_high(series: pd.Series, window: int) -> pd.Series:
    """Rolling maximum over `window` bars."""
    return series.rolling(window=window, min_periods=window).max()


def rolling_low(series: pd.Series, window: int) -> pd.Series:
    """Rolling minimum over `window` bars."""
    return series.rolling(window=window, min_periods=window).min()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Average True Range using Wilder's smoothing (EMA alpha=1/window).

    TR = max(H-L, |H-PrevC|, |L-PrevC|). First bar has no prev close so TR=H-L.
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Average Directional Index — trend strength in [0, 100].

    Values > 20 indicate a trending market. Uses Wilder's smoothing (alpha = 1/window).
    """
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    dm_plus_raw = high - prev_high
    dm_minus_raw = prev_low - low

    # Each directional move only counts when it's larger than the opposite move.
    dm_plus = dm_plus_raw.where(
        (dm_plus_raw > dm_minus_raw) & (dm_plus_raw > 0.0), 0.0
    )
    dm_minus = dm_minus_raw.where(
        (dm_minus_raw > dm_plus_raw) & (dm_minus_raw > 0.0), 0.0
    )

    atr_val = atr(high, low, close, window)

    alpha = 1.0 / window
    smooth_dm_plus = dm_plus.ewm(alpha=alpha, min_periods=window, adjust=False).mean()
    smooth_dm_minus = dm_minus.ewm(alpha=alpha, min_periods=window, adjust=False).mean()

    safe_atr = atr_val.replace(0.0, float("nan"))
    di_plus = 100.0 * smooth_dm_plus / safe_atr
    di_minus = 100.0 * smooth_dm_minus / safe_atr

    di_sum = (di_plus + di_minus).replace(0.0, float("nan"))
    dx = 100.0 * (di_plus - di_minus).abs() / di_sum
    return dx.ewm(alpha=alpha, min_periods=window, adjust=False).mean()


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_n: int = 14,
    multiplier: float = 3.0,
) -> tuple[pd.Series, pd.Series]:
    """SuperTrend indicator (ATR 14, multiplier 3 by default).

    Returns (supertrend_line, direction) where direction is +1.0 (uptrend) or
    -1.0 (downtrend). Both Series are NaN until ATR warmup is complete.

    In uptrend: supertrend_line is the lower ATR band (support below close).
    In downtrend: supertrend_line is the upper ATR band (resistance above close).
    """
    hl2 = (high + low) / 2.0
    atr_val = atr(high, low, close, atr_n)

    basic_upper = (hl2 + multiplier * atr_val).values.tolist()
    basic_lower = (hl2 - multiplier * atr_val).values.tolist()
    close_list = close.values.tolist()
    n = len(close)

    final_upper = [float("nan")] * n
    final_lower = [float("nan")] * n
    st = [float("nan")] * n
    direction = [float("nan")] * n

    for i in range(n):
        bu = basic_upper[i]
        bl = basic_lower[i]
        if bu != bu:  # NaN check (bu != bu is True only for NaN)
            continue

        # Ratchet bands: upper only moves down, lower only moves up.
        if i == 0 or final_upper[i - 1] != final_upper[i - 1]:
            final_upper[i] = bu
            final_lower[i] = bl
        else:
            final_upper[i] = bu if (bu < final_upper[i - 1] or close_list[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
            final_lower[i] = bl if (bl > final_lower[i - 1] or close_list[i - 1] < final_lower[i - 1]) else final_lower[i - 1]

        # Determine trend direction.
        if i == 0 or st[i - 1] != st[i - 1]:
            # First valid bar: initialize by comparing close to midband.
            midband = (final_upper[i] + final_lower[i]) / 2.0
            if close_list[i] >= midband:
                st[i] = final_lower[i]
                direction[i] = 1.0
            else:
                st[i] = final_upper[i]
                direction[i] = -1.0
        elif direction[i - 1] == -1.0:
            # Was downtrend: flip up if close breaks above upper band.
            if close_list[i] > final_upper[i]:
                st[i] = final_lower[i]
                direction[i] = 1.0
            else:
                st[i] = final_upper[i]
                direction[i] = -1.0
        else:
            # Was uptrend: flip down if close breaks below lower band.
            if close_list[i] < final_lower[i]:
                st[i] = final_upper[i]
                direction[i] = -1.0
            else:
                st[i] = final_lower[i]
                direction[i] = 1.0

    return (
        pd.Series(st, index=close.index),
        pd.Series(direction, index=close.index),
    )
