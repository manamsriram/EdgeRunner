"""Performance metrics for a backtest equity curve.

Reports the numbers that actually matter for judging edge — risk-adjusted return and
drawdown, not just total return — and always alongside a buy-and-hold baseline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass(frozen=True)
class Metrics:
    total_return: float
    annualized_return: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    win_rate: float
    turnover: int          # number of round-trip trades
    final_equity: float
    ic: float | None = None
    icir: float | None = None

    def as_dict(self) -> dict:
        return {
            "total_return": self.total_return,
            "annualized_return": self.annualized_return,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "calmar": self.calmar,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
            "turnover": self.turnover,
            "final_equity": self.final_equity,
            "ic": self.ic,
            "icir": self.icir,
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


def _sortino(equity: pd.Series) -> float:
    """Sharpe variant penalising downside vol only."""
    if len(equity) < 2:
        return 0.0
    returns = equity.pct_change().dropna()
    downside_std = returns[returns < 0].std()
    if downside_std == 0 or np.isnan(downside_std):
        return 0.0
    return float(returns.mean() / downside_std * np.sqrt(TRADING_DAYS))


def _calmar(equity: pd.Series) -> float:
    """Annualised return divided by max drawdown magnitude."""
    if len(equity) < 2:
        return 0.0
    returns = equity.pct_change().dropna()
    ann_return = (1 + returns.mean()) ** TRADING_DAYS - 1
    mdd = _max_drawdown(equity)  # negative value
    if mdd == 0 or np.isnan(mdd):
        return 0.0
    return float(ann_return / abs(mdd))


def _beta(
    strategy_equity: pd.Series,
    benchmark_equity: pd.Series,
    window: int = 60,
) -> float:
    """Average rolling 60-day beta of strategy vs benchmark.

    Both series must share a DatetimeIndex. Returns nan when fewer than 10
    aligned observations are available (e.g. very short backtests).
    """
    strat_r = strategy_equity.pct_change().dropna()
    bench_r = benchmark_equity.pct_change().reindex(strat_r.index).dropna()
    aligned = pd.concat([strat_r, bench_r], axis=1).dropna()
    if len(aligned) < 10:
        return float("nan")
    cov = aligned.iloc[:, 0].rolling(window, min_periods=10).cov(aligned.iloc[:, 1])
    var = aligned.iloc[:, 1].rolling(window, min_periods=10).var()
    return float((cov / var).mean())


def compute_metrics(equity: pd.Series, trades: list) -> Metrics:
    """`trades` is a list of objects exposing `.return_pct` (e.g. engine.Trade)."""
    if equity.empty:
        return Metrics(
            total_return=0.0,
            annualized_return=0.0,
            sharpe=0.0,
            sortino=0.0,
            calmar=0.0,
            max_drawdown=0.0,
            win_rate=0.0,
            turnover=0,
            final_equity=0.0,
        )

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
        sortino=_sortino(equity),
        calmar=_calmar(equity),
        max_drawdown=_max_drawdown(equity),
        win_rate=win_rate,
        turnover=len(trades),
        final_equity=end,
    )


def compute_ic_from_backtest_fills(
    fills: list[dict],
    bars: pd.DataFrame,
) -> tuple[float | None, float | None]:
    """Compute (ic, icir) from backtest fills.

    Pairs each buy fill's signal_strength with the next-bar return:
      next_bar_return = (close[fill_date + 1] / close[fill_date]) - 1

    Returns (None, None) on any error or insufficient data.
    Uses backtest fill keys: 'date', 'side', 'signal_strength'.
    """
    buy_fills = [f for f in fills if f.get("side") == "buy"
                 and f.get("signal_strength") is not None]
    if len(buy_fills) < 5:
        return None, None

    strengths: list[float] = []
    next_returns: list[float] = []

    closes = bars["close"] if "close" in bars.columns else bars.get("Close")
    if closes is None:
        return None, None

    for f in buy_fills:
        date = f["date"]
        try:
            idx = closes.index.get_loc(date)
        except KeyError:
            continue
        if idx + 1 >= len(closes):
            continue
        ret = float(closes.iloc[idx + 1] / closes.iloc[idx] - 1)
        strengths.append(float(f["signal_strength"]))
        next_returns.append(ret)

    from trader.learning.ic_metrics import compute_ic, compute_icir
    ic = compute_ic(strengths, next_returns)
    icir = compute_icir([ic] if ic is not None else [])
    return ic, icir


def format_report(
    result,
    strategy_name: str,
    symbol: str,
    benchmark_equity: Optional[pd.Series] = None,
    bars: Optional[pd.DataFrame] = None,
    validate: bool = False,
) -> str:
    """Human-readable report comparing the strategy to buy-and-hold.

    `result` is a BacktestResult. Pass `benchmark_equity` (e.g. SPY equity curve
    over the same date range) to include a beta row; omit for crypto or when SPY
    data is unavailable. Caveats travel with every report rather than living only
    in the docs.

    `validate=True` appends a statistical-significance block (permutation p-values,
    bootstrap Sharpe CI, walk-forward consistency) so a headline Sharpe can't be
    read without its "is this luck?" context. Off by default — the permutation and
    bootstrap resampling cost ~1000 recomputes each, so opt in for a single chosen
    result, not inside a sweep.
    """
    strat = compute_metrics(result.equity_curve, result.trades)
    if bars is not None:
        import dataclasses
        ic, icir = compute_ic_from_backtest_fills(result.fills, bars)
        strat = dataclasses.replace(strat, ic=ic, icir=icir)
    bh = compute_metrics(result.buy_hold_curve, [])
    lines = [
        f"Backtest: {strategy_name} on {symbol}",
        "-" * 52,
        f"{'metric':<20}{'strategy':>16}{'buy & hold':>16}",
        f"{'total return':<20}{strat.total_return:>16.1%}{bh.total_return:>16.1%}",
        f"{'annualized':<20}{strat.annualized_return:>16.1%}{bh.annualized_return:>16.1%}",
        f"{'sharpe':<20}{strat.sharpe:>16.2f}{bh.sharpe:>16.2f}",
        f"{'sortino':<20}{strat.sortino:>16.2f}{bh.sortino:>16.2f}",
        f"{'calmar':<20}{strat.calmar:>16.2f}{bh.calmar:>16.2f}",
        f"{'max drawdown':<20}{strat.max_drawdown:>16.1%}{bh.max_drawdown:>16.1%}",
        f"{'win rate':<20}{strat.win_rate:>16.1%}{'n/a':>16}",
        f"{'round trips':<20}{strat.turnover:>16d}{0:>16d}",
        f"{'final equity':<20}{strat.final_equity:>16.0f}{bh.final_equity:>16.0f}",
    ]
    if strat.ic is not None:
        lines.append(f"{'IC':<20}{strat.ic:>16.3f}{'':>16}")
    if strat.icir is not None:
        lines.append(f"{'ICIR':<20}{strat.icir:>16.3f}{'':>16}")
    if benchmark_equity is not None:
        b = _beta(result.equity_curve, benchmark_equity)
        beta_str = f"{b:.2f}" if not np.isnan(b) else "n/a"
        lines.append(f"{'beta vs SPY':<20}{beta_str:>16}{'n/a':>16}")
    if validate:
        lines += _validation_lines(result)
    lines += [
        "-" * 52,
        f"slippage {result.cost_model.slippage_bps:.0f}bps, "
        f"commission ${result.cost_model.commission_per_trade:.2f}/trade",
        "Caveats: free data is survivorship-biased (delisted names absent); paper/live",
        "fills are optimistic; fundamentals are not point-in-time. Price/technical only.",
    ]
    return "\n".join(lines)


def _validation_lines(result) -> list[str]:
    """Format the statistical-validation block. Local import avoids the
    metrics <-> validation import cycle (validation imports TRADING_DAYS)."""
    from trader.backtest.validation import validate_result

    v = validate_result(result)
    perm, boot, wf = v["permutation"], v["bootstrap"], v["walk_forward"]
    out = ["-" * 52, "validation (is the edge real, or luck?)"]
    if "error" in perm:
        out.append(f"  permutation:   n/a ({perm['error']})")
    else:
        out.append(f"  permutation:   p(sharpe)={perm['p_value_sharpe']:.3f}  "
                   f"p(maxDD)={perm['p_value_max_dd']:.3f}  (lower = less likely luck)")
    if "error" in boot:
        out.append(f"  bootstrap:     n/a ({boot['error']})")
    else:
        out.append(f"  bootstrap:     sharpe {boot['sharpe']:.2f} "
                   f"[{boot['ci_low']:.2f}, {boot['ci_high']:.2f}] 95%CI  "
                   f"p(<=0)={boot['p_value_positive']:.3f}")
    if "error" in wf:
        out.append(f"  walk-forward:  n/a ({wf['error']})")
    else:
        out.append(f"  walk-forward:  {wf['profitable_windows']}/{wf['n_windows']} "
                   f"windows profitable (consistency {wf['consistency_rate']:.0%})")
    return out
