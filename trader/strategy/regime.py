"""Volatility regime detection.

Classifies the current market regime from the same daily bars strategies already
receive: 20-day realized volatility ranked against its own trailing one-year
distribution. The result is a deterministic function of price history, so the
backtest engine replays exactly what the live pipeline sees — no live-only state.

Regimes:
    calm     — current vol below the 33rd percentile of the trailing year
    normal   — middle band, or insufficient history (degrades to the baseline)
    stressed — current vol above the 67th percentile of the trailing year
"""
from __future__ import annotations

import numpy as np
import pandas as pd

Regime = str  # "calm" | "normal" | "stressed"

REALIZED_VOL_WINDOW = 20
PERCENTILE_LOOKBACK = 252
CALM_PERCENTILE = 0.33
STRESSED_PERCENTILE = 0.67
# One year of vol observations plus the window needed to compute the first one.
MIN_REGIME_BARS = PERCENTILE_LOOKBACK + REALIZED_VOL_WINDOW + 1

TRADING_DAYS_PER_YEAR = 252


def realized_vol(close: pd.Series, window: int = REALIZED_VOL_WINDOW) -> pd.Series:
    """Annualized rolling realized volatility from log returns."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(window).std() * np.sqrt(TRADING_DAYS_PER_YEAR)


def classify_regime(bars: pd.DataFrame) -> Regime:
    """Classify the volatility regime as of the last bar.

    Returns "normal" when history is too short to rank the current vol against a
    full trailing year, so callers fall back to baseline parameters rather than
    acting on a poorly estimated distribution.
    """
    if len(bars) < MIN_REGIME_BARS:
        return "normal"

    vol = realized_vol(bars["close"])
    curr = float(vol.iloc[-1])
    history = vol.iloc[-PERCENTILE_LOOKBACK:].dropna()
    if np.isnan(curr) or history.empty:
        return "normal"

    # Midrank percentile: ties count half, so a flat vol series ranks 0.5
    # (normal) instead of 1.0 (stressed).
    pctl = float(((history < curr).mean() + (history <= curr).mean()) / 2.0)
    if pctl < CALM_PERCENTILE:
        return "calm"
    if pctl > STRESSED_PERCENTILE:
        return "stressed"
    return "normal"
