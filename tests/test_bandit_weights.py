"""Unit tests for trader.learning.bandit_weights — pure EWMA arm-weighting logic.

No DB, no network — all functions operate on plain floats/lists.
"""
from __future__ import annotations

from trader.learning.bandit_weights import (
    DEFAULT_WEIGHT,
    WEIGHT_CEIL,
    WEIGHT_FLOOR,
    apply_forced_exploration,
    ewma_weight,
)


def test_below_min_samples_returns_default_weight():
    pnls = [10.0, -5.0, 3.0]  # only 3 trades, below MIN_SAMPLES=20
    result = ewma_weight(prev_weight=0.6, pnls=pnls, min_samples=20)
    assert result == DEFAULT_WEIGHT


def test_high_win_rate_pulls_weight_above_default():
    pnls = [10.0] * 18 + [-5.0] * 2  # 20 trades, 90% win rate
    result = ewma_weight(prev_weight=1.0, pnls=pnls, min_samples=20, alpha=0.5)
    assert result > DEFAULT_WEIGHT


def test_low_win_rate_pulls_weight_below_default():
    pnls = [-10.0] * 18 + [5.0] * 2  # 20 trades, 10% win rate
    result = ewma_weight(prev_weight=1.0, pnls=pnls, min_samples=20, alpha=0.5)
    assert result < DEFAULT_WEIGHT


def test_weight_never_exceeds_ceiling():
    pnls = [10.0] * 50  # 100% win rate, repeated extreme EWMA pulls
    result = ewma_weight(prev_weight=WEIGHT_CEIL, pnls=pnls, min_samples=20, alpha=1.0)
    assert result <= WEIGHT_CEIL


def test_weight_never_drops_below_floor():
    pnls = [-10.0] * 50  # 0% win rate, repeated extreme EWMA pulls
    result = ewma_weight(prev_weight=WEIGHT_FLOOR, pnls=pnls, min_samples=20, alpha=1.0)
    assert result >= WEIGHT_FLOOR


def test_forced_exploration_resets_to_default_on_schedule():
    # cycle_index 10 with every=10 should force-reset regardless of computed weight
    result = apply_forced_exploration(weight=0.5, cycle_index=10, every=10)
    assert result == DEFAULT_WEIGHT


def test_forced_exploration_noop_otherwise():
    result = apply_forced_exploration(weight=0.7, cycle_index=3, every=10)
    assert result == 0.7
