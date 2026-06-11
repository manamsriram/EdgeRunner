"""Tests for vol-targeted position sizing."""
import numpy as np
import pandas as pd
import pytest

from trader.risk.vol_sizing import DEFAULT_FLOOR, DEFAULT_TARGET_VOL, vol_scale


def _bars(returns: np.ndarray, start_price: float = 100.0) -> pd.DataFrame:
    closes = start_price * np.cumprod(1.0 + returns)
    idx = pd.bdate_range("2023-01-02", periods=len(closes))
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": 1_000_000,
        },
        index=idx,
    )


def _alternating(n: int, magnitude: float) -> np.ndarray:
    return np.where(np.arange(n) % 2 == 0, magnitude, -magnitude)


def test_insufficient_history_returns_full_size():
    bars = _bars(_alternating(5, 0.01))
    assert vol_scale(bars) == 1.0


def test_zero_vol_returns_full_size():
    bars = _bars(np.zeros(60))
    assert vol_scale(bars) == 1.0


def test_quiet_market_is_full_size():
    # ~0.0005 daily swings → ~0.8% annualized vol, far under any target.
    bars = _bars(_alternating(60, 0.0005))
    assert vol_scale(bars) == 1.0


def test_loud_market_is_scaled_down():
    # ~0.03 daily swings → ~48% annualized vol, well above a 20% target.
    bars = _bars(_alternating(60, 0.03))
    scale = vol_scale(bars, target_vol=0.20)
    assert scale < 1.0
    assert scale >= DEFAULT_FLOOR


def test_floor_clamps_extreme_vol():
    bars = _bars(_alternating(60, 0.10))  # absurdly volatile
    assert vol_scale(bars, target_vol=0.10, floor=0.25) == 0.25


def test_higher_vol_means_smaller_scale():
    calm = _bars(_alternating(60, 0.012))
    loud = _bars(_alternating(60, 0.02))
    assert vol_scale(loud, target_vol=0.20) < vol_scale(calm, target_vol=0.20)


def test_scale_matches_target_over_realized():
    from trader.strategy.regime import realized_vol

    bars = _bars(_alternating(60, 0.02))
    realized = float(realized_vol(bars["close"]).iloc[-1])
    expected = 0.20 / realized
    assert vol_scale(bars, target_vol=0.20, floor=0.0) == pytest.approx(expected)


def test_defaults_are_sane():
    assert 0.0 < DEFAULT_FLOOR < 1.0
    assert 0.0 < DEFAULT_TARGET_VOL < 1.0
