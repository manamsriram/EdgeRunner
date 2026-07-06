"""Tests for the smooth_score indicator helper."""
from __future__ import annotations

import pandas as pd

from trader.strategy.indicators import smooth_score


def test_int_window_is_rolling_mean() -> None:
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = smooth_score(series, 3)
    assert result.iloc[-1] == (3.0 + 4.0 + 5.0) / 3.0


def test_float_window_is_ewma() -> None:
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = smooth_score(series, 3.0)
    expected = series.ewm(span=3.0, min_periods=1, adjust=False).mean()
    pd.testing.assert_series_equal(result, expected)


def test_output_same_length_as_input() -> None:
    series = pd.Series(range(10), dtype=float)
    assert len(smooth_score(series, 4)) == len(series)


def test_damps_single_spike() -> None:
    series = pd.Series([0.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0])
    smoothed = smooth_score(series, 5)
    assert smoothed.iloc[4] < series.iloc[4]
