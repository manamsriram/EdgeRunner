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
    # Out-of-sample metrics on the held-out tail when holdout_frac > 0; None otherwise.
    # `metrics` above is then the TRAIN slice used for ranking. Compare the two: a
    # combo whose holdout metric collapses vs its train metric is overfit to the grid.
    holdout_metrics: Metrics | None = None


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
    holdout_frac: float = 0.0,
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

    `holdout_frac` (0..1) reserves the final fraction of bars as an out-of-sample
    test set. Ranking is then done on the TRAIN slice (`SweepResult.metrics`) while
    each combo's OOS performance is reported in `SweepResult.holdout_metrics`. This is
    the real defense against grid overfitting: an exhaustive in-sample search will
    always find a top Sharpe inflated ~sqrt(2*ln K) for a grid of K combos, so trust
    the holdout, not the winning in-sample number.
    """
    if not 0.0 <= holdout_frac < 1.0:
        raise ValueError(f"holdout_frac must be in [0, 1), got {holdout_frac}")

    if holdout_frac > 0.0:
        split = int(len(bars) * (1.0 - holdout_frac))
        train_bars, holdout_bars = bars.iloc[:split], bars.iloc[split:]
    else:
        train_bars, holdout_bars = bars, None

    def _run(strategy, frame):
        return run_backtest(
            frame, strategy, initial_cash=initial_cash,
            cost_model=cost_model, stop_loss_pct=stop_loss_pct,
        )

    keys = list(param_grid.keys())
    results: list[SweepResult] = []
    for combo in product(*(param_grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        try:
            strategy = strategy_factory(**params)
        except ValueError:
            continue
        result = _run(strategy, train_bars)
        m = compute_metrics(result.equity_curve, result.trades)

        holdout_metrics = None
        holdout_result = None
        if holdout_bars is not None and len(holdout_bars) >= 2:
            # Fresh instance: strategies may carry per-run state from the train pass.
            holdout_result = _run(strategy_factory(**params), holdout_bars)
            holdout_metrics = compute_metrics(
                holdout_result.equity_curve, holdout_result.trades
            )

        consistency = None
        if validate:
            from trader.backtest.validation import walk_forward
            # Consistency on the holdout when we have one, else on the (train) run.
            wf_src = holdout_result or result
            wf = walk_forward(wf_src.equity_curve, wf_src.trades, n_windows=n_windows)
            consistency = wf.get("consistency_rate")  # None on too-short windows

        results.append(SweepResult(
            params=params, metrics=m, consistency=consistency,
            holdout_metrics=holdout_metrics,
        ))

    results.sort(key=lambda r: getattr(r.metrics, metric), reverse=True)
    return results
