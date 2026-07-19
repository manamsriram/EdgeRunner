"""Shared price/vol statistics — one source of truth for the LLM overlay's
prompt context and the ML-overlay feature builder, so the two never drift.
"""
from __future__ import annotations

import pandas as pd


def compute_bar_stats(bars: pd.DataFrame) -> dict:
    """Derive price/vol stats from an already asof-truncated bars DataFrame.

    Returns {} if there isn't enough history (mirrors the LLM prompt's own
    "insufficient bar data" fallback).
    """
    if bars.empty or len(bars) < 2:
        return {}
    close = bars["close"]
    last_close = float(close.iloc[-1])
    lookback_20 = min(20, len(close) - 1)
    pct_20d = float((close.iloc[-1] / close.iloc[-(lookback_20 + 1)] - 1) * 100)
    lookback_10 = min(10, len(close) - 1)
    returns_10 = close.pct_change().dropna().iloc[-lookback_10:]
    vol_10d = float(returns_10.std() * (252 ** 0.5) * 100) if len(returns_10) > 1 else 0.0
    return {
        "last_close": last_close,
        "pct_20d": pct_20d,
        "vol_10d_annualized": vol_10d,
        "n_days": len(close),
        "lookback_20": lookback_20,
        "lookback_10": lookback_10,
    }
