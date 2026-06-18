"""Per-(strategy, regime) ranking-priority weights, updated nightly from realized trade P&L.

Shadow-mode only until validated: see CLAUDE.md / plan notes. Never mutates
Signal.strength — produces a separate multiplier applied at ranking time.
"""
from __future__ import annotations

import numpy as np

DEFAULT_WEIGHT = 1.0
WEIGHT_FLOOR = 0.5
WEIGHT_CEIL = 1.5


def _win_rate(pnls: list[float]) -> float:
    if not pnls:
        return 0.5
    wins = sum(1 for p in pnls if p > 0)
    return wins / len(pnls)


def ewma_weight(
    prev_weight: float,
    pnls: list[float],
    min_samples: int = 20,
    alpha: float = 0.3,
    floor: float = WEIGHT_FLOOR,
    ceil: float = WEIGHT_CEIL,
) -> float:
    """Update an arm's weight from a batch of realized trade P&Ls.

    Below `min_samples`, returns DEFAULT_WEIGHT unconditionally — small-n
    samples are noise, not signal (see 2026-06-10 regime-adaptive failure).
    Kept as backward-compat shim; new code uses thompson_sample.
    """
    if len(pnls) < min_samples:
        return DEFAULT_WEIGHT

    target = floor + _win_rate(pnls) * (ceil - floor)
    updated = alpha * target + (1 - alpha) * prev_weight
    return max(floor, min(ceil, updated))


def apply_forced_exploration(weight: float, cycle_index: int, every: int = 10) -> float:
    """Every `every`-th nightly cycle, reset to DEFAULT_WEIGHT so a converged
    low-weight arm still gets full priority and keeps generating fresh data."""
    if every > 0 and cycle_index % every == 0:
        return DEFAULT_WEIGHT
    return weight


# ---- Thompson Sampling ----

def thompson_sample(alpha: int, beta: int, rng: np.random.Generator | None = None) -> float:
    """Sample θ ~ Beta(α, β) → scale to [FLOOR, CEIL]. rng=None uses np.random.default_rng()."""
    rng = rng or np.random.default_rng()
    theta = float(rng.beta(alpha, beta))
    return WEIGHT_FLOOR + theta * (WEIGHT_CEIL - WEIGHT_FLOOR)


def update_arm(alpha: int, beta: int, pnls: list[float]) -> tuple[int, int]:
    """Win (pnl > 0) → alpha+1, loss → beta+1. Returns new (alpha, beta)."""
    wins = sum(1 for p in pnls if p > 0)
    losses = len(pnls) - wins
    return alpha + wins, beta + losses


def should_reset(cycle_index: int, every: int = 10) -> bool:
    """Return True when cycle_index is a non-zero multiple of every (every=0 disables)."""
    return every > 0 and cycle_index > 0 and cycle_index % every == 0
