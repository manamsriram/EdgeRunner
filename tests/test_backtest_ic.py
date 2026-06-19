import pandas as pd
import pytest
from trader.backtest.metrics import compute_ic_from_backtest_fills


def _make_bars(prices):
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="B")
    return pd.DataFrame({"close": prices, "open": prices, "high": prices, "low": prices}, index=idx)


def test_compute_ic_from_backtest_fills_basic():
    bars = _make_bars([100, 105, 110, 115, 120, 125, 130])
    dates = bars.index
    fills = [
        {"date": dates[0], "side": "buy", "signal_strength": 0.8},
        {"date": dates[1], "side": "buy", "signal_strength": 0.6},
        {"date": dates[2], "side": "buy", "signal_strength": 0.9},
        {"date": dates[3], "side": "buy", "signal_strength": 0.7},
        {"date": dates[4], "side": "buy", "signal_strength": 0.5},
    ]
    ic, icir = compute_ic_from_backtest_fills(fills, bars)
    assert ic is not None  # should compute something with 5 pairs
    assert isinstance(ic, float)


def test_compute_ic_from_backtest_fills_returns_none_too_few():
    bars = _make_bars([100, 105, 110])
    dates = bars.index
    fills = [
        {"date": dates[0], "side": "buy", "signal_strength": 0.8},
        {"date": dates[1], "side": "buy", "signal_strength": 0.6},
    ]
    ic, icir = compute_ic_from_backtest_fills(fills, bars)
    assert ic is None
    assert icir is None


def test_compute_ic_skips_fills_without_strength():
    bars = _make_bars([100, 105, 110, 115, 120, 125, 130])
    dates = bars.index
    fills = [
        {"date": dates[0], "side": "buy", "signal_strength": None},  # no strength
        {"date": dates[1], "side": "sell", "signal_strength": 0.6},  # sell — skip
        {"date": dates[2], "side": "buy", "signal_strength": 0.9},
        {"date": dates[3], "side": "buy", "signal_strength": 0.7},
        {"date": dates[4], "side": "buy", "signal_strength": 0.5},
    ]
    # Only 3 valid buy fills with strength — should return None
    ic, icir = compute_ic_from_backtest_fills(fills, bars)
    assert ic is None
