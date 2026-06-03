"""Performance metrics for a backtest equity curve.

Reports the numbers that actually matter for judging edge — risk-adjusted return and
drawdown, not just total return — and always alongside a buy-and-hold baseline.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass(frozen=True)
class Metrics:
    total_return: float
    annualized_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    turnover: int          # number of round-trip trades
    final_equity: float

    def as_dict(self) -> dict:
        return {
            "total_return": self.total_return,
            "annualized_return": self.annualized_return,
            "sharpe": self.sharpe,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
            "turnover": self.turnover,
            "final_equity": self.final_equity,
        }


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    return float(drawdown.min())


def _sharpe(equity: pd.Series) -> float:
    if len(equity) < 2:
        return 0.0
    returns = equity.pct_change().dropna()
    std = returns.std()
    if std == 0 or np.isnan(std):
        return 0.0
    return float(returns.mean() / std * np.sqrt(TRADING_DAYS))


def compute_metrics(equity: pd.Series, trades: list) -> Metrics:
    """`trades` is a list of objects exposing `.return_pct` (e.g. engine.Trade)."""
    if equity.empty:
        return Metrics(0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0)

    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    total_return = end / start - 1.0
    periods = max(len(equity) - 1, 1)
    annualized = (1.0 + total_return) ** (TRADING_DAYS / periods) - 1.0
    wins = sum(1 for t in trades if t.return_pct > 0)
    win_rate = wins / len(trades) if trades else 0.0

    return Metrics(
        total_return=total_return,
        annualized_return=float(annualized),
        sharpe=_sharpe(equity),
        max_drawdown=_max_drawdown(equity),
        win_rate=win_rate,
        turnover=len(trades),
        final_equity=end,
    )


def format_report(result, strategy_name: str, symbol: str) -> str:
    """Human-readable report comparing the strategy to buy-and-hold.

    `result` is a BacktestResult. Includes the standard honesty caveats so they travel
    with every report rather than living only in the docs.
    """
    from trader.backtest.metrics import compute_metrics  # local to avoid cycle on import

    strat = compute_metrics(result.equity_curve, result.trades)
    bh = compute_metrics(result.buy_hold_curve, [])
    lines = [
        f"Backtest: {strategy_name} on {symbol}",
        "-" * 56,
        f"{'metric':<20}{'strategy':>16}{'buy & hold':>16}",
        f"{'total return':<20}{strat.total_return:>15.1%}{bh.total_return:>16.1%}",
        f"{'annualized':<20}{strat.annualized_return:>15.1%}{bh.annualized_return:>16.1%}",
        f"{'sharpe':<20}{strat.sharpe:>16.2f}{bh.sharpe:>16.2f}",
        f"{'max drawdown':<20}{strat.max_drawdown:>15.1%}{bh.max_drawdown:>16.1%}",
        f"{'win rate':<20}{strat.win_rate:>15.1%}{'n/a':>16}",
        f"{'round trips':<20}{strat.turnover:>16d}{0:>16d}",
        f"{'final equity':<20}{strat.final_equity:>16.0f}{bh.final_equity:>16.0f}",
        "-" * 56,
        f"slippage {result.cost_model.slippage_bps:.0f}bps, "
        f"commission ${result.cost_model.commission_per_trade:.2f}/trade",
        "Caveats: free data is survivorship-biased (delisted names absent); paper/live",
        "fills are optimistic; fundamentals are not point-in-time. Price/technical only.",
    ]
    return "\n".join(lines)
