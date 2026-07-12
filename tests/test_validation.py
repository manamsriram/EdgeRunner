"""Tests for the backtest statistical-validation suite."""
from __future__ import annotations

import numpy as np
import pandas as pd

from trader.backtest.engine import Trade
from trader.backtest.validation import (
    bootstrap_sharpe_ci,
    permutation_test,
    validate_result,
    walk_forward,
)


def _trade(entry: str, exit_: str, ret: float) -> Trade:
    """Build a Trade with a known return_pct (entry_price=100, exit=100*(1+ret))."""
    return Trade(
        entry_date=pd.Timestamp(entry),
        entry_price=100.0,
        exit_date=pd.Timestamp(exit_),
        exit_price=100.0 * (1.0 + ret),
        shares=1.0,
    )


def _equity(values: list[float]) -> pd.Series:
    idx = pd.bdate_range("2024-01-02", periods=len(values))
    return pd.Series(values, index=idx, name="equity")


# ─── permutation_test ───

def test_permutation_needs_min_trades() -> None:
    out = permutation_test([_trade("2024-01-02", "2024-01-03", 0.01)])
    assert out["p_value_sharpe"] == 1.0
    assert "error" in out


def test_permutation_pvalue_in_unit_interval() -> None:
    trades = [_trade(f"2024-01-{d:02d}", f"2024-01-{d+1:02d}", r)
              for d, r in zip(range(2, 12), [0.02, -0.01, 0.03, 0.01, -0.02,
                                             0.04, 0.01, -0.01, 0.02, 0.03])]
    out = permutation_test(trades, n_simulations=500, seed=1)
    assert 0.0 <= out["p_value_sharpe"] <= 1.0
    assert 0.0 <= out["p_value_max_dd"] <= 1.0
    # Reproducible with a fixed seed.
    assert out == permutation_test(trades, n_simulations=500, seed=1)


def test_permutation_sharpe_low_p_for_strong_edge() -> None:
    # Varied but all-positive returns: a genuine directional edge (positive mean,
    # finite variance) → sign-flip almost never matches the actual Sharpe, so p is
    # near 0. The old order-shuffle gave p≈1 here — the bug this fix addresses.
    rets = [0.03, 0.01, 0.02, 0.04, 0.015, 0.025, 0.035, 0.01, 0.02, 0.03, 0.045, 0.02]
    trades = [_trade(f"2024-02-{d:02d}", f"2024-02-{d+1:02d}", r)
              for d, r in zip(range(2, 14), rets)]
    out = permutation_test(trades, n_simulations=1000, seed=1)
    assert out["p_value_sharpe"] < 0.05


def test_permutation_sharpe_high_p_for_no_edge() -> None:
    # Symmetric zero-mean returns: no edge → sign-flip Sharpe ~ actual → p near 0.5.
    rets = [0.02, -0.02] * 8
    trades = [_trade(f"2024-03-{d:02d}", f"2024-03-{d+1:02d}", r)
              for d, r in zip(range(2, 18), rets)]
    out = permutation_test(trades, n_simulations=2000, seed=1)
    assert 0.2 < out["p_value_sharpe"] < 0.8


# ─── bootstrap_sharpe_ci ───

def test_bootstrap_ci_brackets_point_estimate() -> None:
    # Steady uptrend -> positive Sharpe, CI low should sit below the point est.
    equity = _equity([100 * (1.01 ** i) for i in range(60)])
    out = bootstrap_sharpe_ci(equity, n_bootstrap=500, seed=1)
    assert out["ci_low"] <= out["sharpe"] <= out["ci_high"]
    assert out["p_value_positive"] < 0.5  # mostly-positive Sharpe distribution


def test_bootstrap_short_series_degrades() -> None:
    out = bootstrap_sharpe_ci(_equity([100.0]), n_bootstrap=100)
    assert "error" in out


# ─── walk_forward ───

def test_walk_forward_all_windows_profitable() -> None:
    equity = _equity([100 * (1.005 ** i) for i in range(50)])
    out = walk_forward(equity, [], n_windows=5)
    assert out["n_windows"] == 5
    assert out["consistency_rate"] == 1.0
    assert out["profitable_windows"] == 5


def test_walk_forward_too_short() -> None:
    out = walk_forward(_equity([100.0, 101.0]), [], n_windows=5)
    assert "error" in out


def test_walk_forward_assigns_trades_by_exit() -> None:
    equity = _equity([100.0 + i for i in range(50)])
    trades = [_trade("2024-01-02", "2024-01-03", 0.01),   # window 1
              _trade("2024-03-01", "2024-03-08", -0.01)]  # a later window
    out = walk_forward(equity, trades, n_windows=5)
    total = sum(w["trades"] for w in out["windows"])
    assert total == 2


# ─── validate_result aggregator ───

def test_validate_result_bundles_all_three() -> None:
    class _R:
        equity_curve = _equity([100 * (1.01 ** i) for i in range(60)])
        trades = [_trade(f"2024-01-{d:02d}", f"2024-01-{d+1:02d}", r)
                  for d, r in zip(range(2, 8), [0.02, -0.01, 0.03, 0.01, -0.02, 0.04])]

    out = validate_result(_R(), n_simulations=200, n_bootstrap=200, seed=1)
    assert set(out) == {"permutation", "bootstrap", "walk_forward"}
    assert "consistency_rate" in out["walk_forward"]
