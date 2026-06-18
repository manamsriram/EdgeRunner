"""Unit tests for trader.learning.bandit_weights — pure EWMA arm-weighting logic.

No DB, no network — all functions operate on plain floats/lists.
"""
from __future__ import annotations

import numpy as np

from trader.learning.bandit_weights import (
    DEFAULT_WEIGHT,
    WEIGHT_CEIL,
    WEIGHT_FLOOR,
    apply_forced_exploration,
    ewma_weight,
    should_reset,
    thompson_sample,
    update_arm,
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


# ---- Thompson Sampling ----

def test_thompson_sample_in_floor_ceil_range():
    rng = np.random.default_rng(42)
    for _ in range(100):
        w = thompson_sample(5, 5, rng=rng)
        assert WEIGHT_FLOOR <= w <= WEIGHT_CEIL


def test_thompson_sample_seeded_deterministic():
    rng1 = np.random.default_rng(99)
    rng2 = np.random.default_rng(99)
    assert thompson_sample(3, 7, rng=rng1) == thompson_sample(3, 7, rng=rng2)


def test_thompson_sample_high_alpha_biases_high():
    rng = np.random.default_rng(0)
    results = [thompson_sample(100, 1, rng=rng) for _ in range(50)]
    assert sum(results) / len(results) > (DEFAULT_WEIGHT + WEIGHT_CEIL) / 2


def test_thompson_sample_high_beta_biases_low():
    rng = np.random.default_rng(0)
    results = [thompson_sample(1, 100, rng=rng) for _ in range(50)]
    assert sum(results) / len(results) < (DEFAULT_WEIGHT + WEIGHT_FLOOR) / 2


def test_update_arm_counts_wins_and_losses():
    new_alpha, new_beta = update_arm(1, 1, [10.0, -5.0, 3.0, -2.0])
    assert new_alpha == 3  # 1 + 2 wins
    assert new_beta == 3   # 1 + 2 losses


def test_update_arm_all_wins():
    new_alpha, new_beta = update_arm(1, 1, [5.0, 10.0, 3.0])
    assert new_alpha == 4
    assert new_beta == 1


def test_update_arm_no_pnls_unchanged():
    assert update_arm(2, 3, []) == (2, 3)


def test_should_reset_at_multiple():
    assert should_reset(10, every=10) is True
    assert should_reset(20, every=10) is True


def test_should_reset_not_at_non_multiple():
    assert should_reset(3, every=10) is False
    assert should_reset(11, every=10) is False


def test_should_reset_zero_cycle_no_reset():
    assert should_reset(0, every=10) is False


def test_should_reset_disabled_when_every_zero():
    assert should_reset(10, every=0) is False
    assert should_reset(100, every=0) is False
