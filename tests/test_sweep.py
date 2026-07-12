"""Tests for the generic parameter grid-search sweep harness."""
from __future__ import annotations

import pandas as pd

from trader.backtest.sweep import SweepResult, param_sweep
from trader.strategy.dip_recovery import DipRecovery, MIN_BARS


def _bars() -> pd.DataFrame:
    # Quiet run to an all-time high, then a sustained drawdown, then a full
    # recovery — enough shape to distinguish shallow vs. deep dip_pct grids.
    closes = [100.0] * MIN_BARS + [95.0, 90.0, 85.0, 90.0, 95.0, 100.0, 106.0]
    idx = pd.bdate_range("2024-01-02", periods=len(closes))
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=idx,
    )


def _factory(**params):
    return DipRecovery("TEST", **params)


def test_sweeps_every_combo() -> None:
    grid = {"dip_pct": [0.05, 0.10], "expansion_pct": [0.03, 0.05]}
    results = param_sweep(_bars(), _factory, grid)
    assert len(results) == 4
    assert all(isinstance(r, SweepResult) for r in results)


def test_results_sorted_descending_by_metric() -> None:
    grid = {"dip_pct": [0.05, 0.10, 0.15], "expansion_pct": [0.05]}
    results = param_sweep(_bars(), _factory, grid, metric="total_return")
    returns = [r.metrics.total_return for r in results]
    assert returns == sorted(returns, reverse=True)


def test_invalid_combo_is_skipped_not_raised() -> None:
    # dip_pct=1.5 fails Strategy's own validation; the sweep should skip it,
    # not blow up the whole grid.
    grid = {"dip_pct": [0.10, 1.5], "expansion_pct": [0.05]}
    results = param_sweep(_bars(), _factory, grid)
    assert len(results) == 1
    assert results[0].params["dip_pct"] == 0.10


def test_validate_attaches_consistency() -> None:
    grid = {"dip_pct": [0.05, 0.10], "expansion_pct": [0.05]}
    plain = param_sweep(_bars(), _factory, grid)
    assert all(r.consistency is None for r in plain)  # off by default

    validated = param_sweep(_bars(), _factory, grid, validate=True, n_windows=3)
    # Every combo carries a consistency_rate in [0, 1] (or None if too short).
    assert all(r.consistency is None or 0.0 <= r.consistency <= 1.0
               for r in validated)


def test_each_combo_gets_its_own_params() -> None:
    grid = {"dip_pct": [0.05, 0.10], "expansion_pct": [0.05]}
    results = param_sweep(_bars(), _factory, grid)
    seen = {tuple(sorted(r.params.items())) for r in results}
    assert seen == {
        (("dip_pct", 0.05), ("expansion_pct", 0.05)),
        (("dip_pct", 0.10), ("expansion_pct", 0.05)),
    }
