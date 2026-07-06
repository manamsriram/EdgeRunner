"""Bar-replay backtest harness.

Replays historical bars through the same Strategy interface the live pipeline will use,
filling on the bar *after* the decision bar and charging realistic costs — so backtest
results are an honest (not optimistic) estimate of edge before any real money.
"""
from trader.backtest.costs import CostModel
from trader.backtest.engine import BacktestResult, run_backtest
from trader.backtest.metrics import Metrics, compute_metrics
from trader.backtest.sweep import SweepResult, param_sweep

__all__ = [
    "BacktestResult",
    "CostModel",
    "Metrics",
    "SweepResult",
    "compute_metrics",
    "param_sweep",
    "run_backtest",
]
