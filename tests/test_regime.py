"""Tests for the volatility regime detector."""
import numpy as np
import pandas as pd
import pytest

from trader.strategy.regime import (
    MIN_REGIME_BARS,
    classify_regime,
    realized_vol,
)


def _bars_from_returns(returns: np.ndarray, start_price: float = 100.0) -> pd.DataFrame:
    """Build a daily-bar DataFrame whose closes follow the given simple returns."""
    closes = start_price * np.cumprod(1.0 + returns)
    idx = pd.bdate_range("2022-01-03", periods=len(closes))
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.001,
            "low": closes * 0.999,
            "close": closes,
            "volume": 1_000_000,
        },
        index=idx,
    )


def _alternating_returns(n: int, magnitude: float) -> np.ndarray:
    """Deterministic +/- alternating returns with rolling std ~= magnitude."""
    signs = np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
    return signs * magnitude


class TestRealizedVol:
    def test_constant_price_has_zero_vol(self):
        bars = _bars_from_returns(np.zeros(60))
        vol = realized_vol(bars["close"])
        assert float(vol.iloc[-1]) == pytest.approx(0.0)

    def test_higher_swings_mean_higher_vol(self):
        calm = _bars_from_returns(_alternating_returns(60, 0.002))
        wild = _bars_from_returns(_alternating_returns(60, 0.03))
        assert float(realized_vol(wild["close"]).iloc[-1]) > float(
            realized_vol(calm["close"]).iloc[-1]
        )


class TestClassifyRegime:
    def test_insufficient_history_returns_normal(self):
        bars = _bars_from_returns(_alternating_returns(MIN_REGIME_BARS - 1, 0.01))
        assert classify_regime(bars) == "normal"

    def test_recent_calm_after_volatile_history_is_calm(self):
        returns = np.concatenate(
            [
                _alternating_returns(300, 0.02),  # volatile past
                _alternating_returns(40, 0.001),  # quiet present
            ]
        )
        assert classify_regime(_bars_from_returns(returns)) == "calm"

    def test_recent_spike_after_quiet_history_is_stressed(self):
        returns = np.concatenate(
            [
                _alternating_returns(300, 0.002),  # quiet past
                _alternating_returns(40, 0.03),  # volatile present
            ]
        )
        assert classify_regime(_bars_from_returns(returns)) == "stressed"

    def test_steady_vol_is_normal(self):
        # Flat vol distribution: current vol ties the whole trailing year, which
        # must rank mid-distribution, not as an extreme.
        bars = _bars_from_returns(_alternating_returns(400, 0.01))
        assert classify_regime(bars) == "normal"

    def test_deterministic(self):
        returns = np.concatenate(
            [_alternating_returns(300, 0.002), _alternating_returns(40, 0.03)]
        )
        bars = _bars_from_returns(returns)
        assert classify_regime(bars) == classify_regime(bars.copy())

    def test_returns_one_of_three_labels(self):
        bars = _bars_from_returns(_alternating_returns(400, 0.01))
        assert classify_regime(bars) in {"calm", "normal", "stressed"}
