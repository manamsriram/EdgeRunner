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


def param_sweep(
    bars: pd.DataFrame,
    strategy_factory: Callable[..., Strategy],
    param_grid: dict[str, list[Any]],
    metric: str = "sharpe",
    initial_cash: float = 10_000.0,
    cost_model: CostModel | None = None,
    stop_loss_pct: float | None = None,
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
        results.append(SweepResult(params=params, metrics=m))

    results.sort(key=lambda r: getattr(r.metrics, metric), reverse=True)
    return results
