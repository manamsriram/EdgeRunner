"""Shared synthetic-data fixtures. No network or Alpaca keys required."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def trending_bars() -> pd.DataFrame:
    """A steadily rising OHLCV series — long enough for SMA(50) to warm up."""
    n = 120
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    close = pd.Series(100.0 + np.arange(n) * 0.5, index=dates)
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 1_000_000,
    }, index=dates)
