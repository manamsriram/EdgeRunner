"""Generic parameter grid-search sweep over a single strategy's backtest.

Formalizes what the ad hoc scripts/backtest_*.py files do by hand for specific
strategy combos: given a strategy's tunable knobs as a {name: [values]} grid,
exhaustively backtest every combination and rank by a chosen metric. Grids are
plain Python dicts/lists — never eval()'d — so a config file can't smuggle in
arbitrary code.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any, Callable

import pandas as pd

from trader.backtest.costs import CostModel
from trader.backtest.engine import run_backtest
from trader.backtest.metrics import Metrics, compute_metrics
from trader.strategy.base import Strategy


@dataclass(frozen=True)
class SweepResult:
    params: dict[str, Any]
    metrics: Metrics
    # Walk-forward consistency_rate (fraction of profitable windows) when the sweep
    # runs with validate=True; None otherwise. Lets you re-rank on robustness — a
    # combo with a great in-sample Sharpe but consistency ~0 is an overfit.
    consistency: float | None = None


def param_sweep(
    bars: pd.DataFrame,
    strategy_factory: Callable[..., Strategy],
    param_grid: dict[str, list[Any]],
    metric: str = "sharpe",
    initial_cash: float = 10_000.0,
    cost_model: CostModel | None = None,
    stop_loss_pct: float | None = None,
    validate: bool = False,
    n_windows: int = 5,
) -> list[SweepResult]:
    """Exhaustive grid search over `param_grid`, ranked by `metric` descending.

    `strategy_factory(**params)` must build a fresh Strategy instance per combo —
    each backtest run needs an unshared instance since some strategies carry
    per-call state derived from history, and reusing one across combos with
    different params would leak stale config between runs.

    `metric` must name a field on `Metrics` (e.g. "sharpe", "calmar", "total_return").
    Combos that raise while constructing the strategy (e.g. a validated param
    outside its valid range) are skipped rather than aborting the whole sweep —
    a grid edge landing on an invalid combo shouldn't kill the search.

    `validate=True` attaches each combo's walk-forward consistency_rate to its
    SweepResult.consistency, so the top-of-grid combo can be checked for robustness
    instead of trusted on in-sample metric alone. Only walk-forward runs here (pure
    slicing, cheap); the expensive permutation/bootstrap resampling stays out of the
    grid loop — run those once on the chosen combo via format_report(validate=True).
    """
    keys = list(param_grid.keys())
    results: list[SweepResult] = []
    for combo in product(*(param_grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        try:
            strategy = strategy_factory(**params)
        except ValueError:
            continue
        result = run_backtest(
            bars,
            strategy,
            initial_cash=initial_cash,
            cost_model=cost_model,
            stop_loss_pct=stop_loss_pct,
        )
        m = compute_metrics(result.equity_curve, result.trades)
        consistency = None
        if validate:
            from trader.backtest.validation import walk_forward
            wf = walk_forward(result.equity_curve, result.trades, n_windows=n_windows)
            consistency = wf.get("consistency_rate")  # None on too-short windows
        results.append(SweepResult(params=params, metrics=m, consistency=consistency))

    results.sort(key=lambda r: getattr(r.metrics, metric), reverse=True)
    return results
