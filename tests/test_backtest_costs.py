"""Costs must reduce returns, and the backtest must produce a baseline + metrics."""
from __future__ import annotations

from trader.backtest.costs import CostModel
from trader.backtest.engine import run_backtest
from trader.backtest.metrics import compute_metrics, format_report
from trader.strategy.ma_crossover import MACrossover


def test_costs_reduce_final_equity(trending_bars):
    strat_free = MACrossover("X", fast=20, slow=50)
    strat_costed = MACrossover("X", fast=20, slow=50)

    free = run_backtest(trending_bars, strat_free,
                        cost_model=CostModel(commission_per_trade=0.0, slippage_bps=0.0))
    costed = run_backtest(trending_bars, strat_costed,
                          cost_model=CostModel(commission_per_trade=1.0, slippage_bps=50.0))

    # The strategy enters at least once on this uptrend, so costs must bite.
    assert len(free.fills) >= 1
    assert costed.equity_curve.iloc[-1] < free.equity_curve.iloc[-1]


def test_cost_model_slippage_is_adverse():
    cm = CostModel(slippage_bps=100.0)  # 1%
    assert cm.fill_price(100.0, "buy") == 101.0    # pay more
    assert cm.fill_price(100.0, "sell") == 99.0    # receive less


def test_report_includes_baseline_and_caveats(trending_bars):
    result = run_backtest(trending_bars, MACrossover("X", fast=20, slow=50))
    report = format_report(result, "MACrossover", "X")
    assert "buy & hold" in report
    assert "Caveats" in report
    # Baseline curve is populated for comparison.
    assert not result.buy_hold_curve.empty


def test_metrics_are_finite(trending_bars):
    result = run_backtest(trending_bars, MACrossover("X", fast=20, slow=50))
    m = compute_metrics(result.equity_curve, result.trades)
    for key, value in m.as_dict().items():
        assert value == value, f"{key} is NaN"  # NaN != NaN
