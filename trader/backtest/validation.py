"""Statistical validation for backtest results — is the edge real or luck?

Three independent, dependency-light checks (pure numpy/pandas, no scipy):

  - permutation_test:   Two independent Monte-Carlo tests on the trade returns.
                        (a) Sharpe via a SIGN-FLIP test: under H0 "no directional
                        edge", each trade's return sign is flipped at random; the
                        p-value is the fraction of sign-flipped samples whose Sharpe
                        is at least the observed one. (Plain order-shuffling cannot
                        test Sharpe — mean and std are permutation-invariant, so it
                        always returns p≈1.) (b) Max drawdown via order-shuffle, which
                        IS order-sensitive and catches a lucky/unlucky sequence.
  - bootstrap_sharpe_ci: Stationary block bootstrap of daily returns to put a
                        confidence interval on Sharpe and estimate P(Sharpe <= 0).
                        Blocks preserve serial correlation, so the CI is not
                        artificially tight the way iid resampling would make it.
  - walk_forward:       Split the equity curve into N sequential windows and report how
                        many are profitable. In-sample-only edges (the overfitting that
                        cost EdgeRunner its regime-adaptive and vol-targeting attempts)
                        show a low consistency_rate here.

These operate on the outputs of trader.backtest.engine.run_backtest — an equity Series
and a list of Trade objects (each exposing .return_pct, .entry_date, .exit_date) — so
nothing here needs the strategy, bars, or a network.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from trader.backtest.metrics import TRADING_DAYS

MIN_TRADES = 3


def _sharpe_from_returns(returns: np.ndarray, annualize: bool = True) -> float:
    """Mean/std of a returns array, optionally annualised by sqrt(252)."""
    if returns.size < 2:
        return 0.0
    std = returns.std()
    if std == 0 or np.isnan(std):
        return 0.0
    ratio = returns.mean() / std
    return float(ratio * np.sqrt(TRADING_DAYS)) if annualize else float(ratio)


def _path_max_dd(returns: np.ndarray) -> float:
    """Max drawdown (negative) of the equity path built by compounding `returns`."""
    equity = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(equity)
    return float(((equity - peak) / peak).min())


# ─── Monte-Carlo permutation ───

def permutation_test(
    trades: list,
    n_simulations: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """Is the actual path better than chance? Two independent Monte-Carlo tests.

    Uses per-trade returns (`trade.return_pct`). Path Sharpe is the raw
    (un-annualised) mean/std of the trade-return sequence — annualisation is a
    constant factor that cancels in the p-value, and trades are not daily.

    Sharpe (sign-flip): under H0 "no directional edge", each trade's return sign is
    flipped with probability 0.5. p_value_sharpe is the fraction of sign-flipped
    samples with Sharpe >= actual. A plain order-shuffle CANNOT test this — mean/std
    are permutation-invariant, so it would report p≈1 for every strategy. Caveat: a
    stop-loss truncates losers and lets winners run, so real trade returns are
    asymmetric; the sign-flip null is therefore approximate, not exact.

    Max drawdown (order-shuffle): drawdown depends on the sequence, so shuffling the
    order is the right null. p_value_max_dd is the fraction of shuffles whose
    drawdown is no worse than actual.
    """
    if len(trades) < MIN_TRADES:
        return {"error": f"need at least {MIN_TRADES} trades", "p_value_sharpe": 1.0}

    returns = np.array([t.return_pct for t in trades], dtype=float)
    actual_sharpe = _sharpe_from_returns(returns, annualize=False)
    actual_max_dd = _path_max_dd(returns)

    rng = np.random.default_rng(seed)
    n = returns.size
    sharpe_ge = 0
    dd_ge = 0  # count shuffles whose drawdown is no worse (>=, less negative-or-equal)
    for _ in range(n_simulations):
        signs = rng.choice((-1.0, 1.0), size=n)
        if _sharpe_from_returns(returns * signs, annualize=False) >= actual_sharpe:
            sharpe_ge += 1
        if _path_max_dd(rng.permutation(returns)) >= actual_max_dd:
            dd_ge += 1

    return {
        "actual_sharpe": round(actual_sharpe, 4),
        "p_value_sharpe": round(sharpe_ge / n_simulations, 4),
        "actual_max_dd": round(actual_max_dd, 6),
        "p_value_max_dd": round(dd_ge / n_simulations, 4),
        "n_simulations": n_simulations,
    }


# ─── Bootstrap Sharpe CI ───

def bootstrap_sharpe_ci(
    equity_curve: pd.Series,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
    seed: int = 42,
) -> dict[str, Any]:
    """Stationary block bootstrap of daily returns to bound Sharpe and test it vs 0.

    Resampling in blocks (rather than single iid draws) preserves the serial
    correlation of an equity curve, so the confidence interval is not artificially
    narrow. Block length defaults to ~n**(1/3) (min 2), the standard rule of thumb.
    """
    if len(equity_curve) < 3:
        return {"error": "need at least 3 equity points"}

    returns = equity_curve.pct_change().dropna().to_numpy()
    if returns.size < 2:
        return {"error": "not enough returns"}

    point = _sharpe_from_returns(returns)
    rng = np.random.default_rng(seed)
    n = returns.size
    block = max(2, round(n ** (1.0 / 3.0)))
    samples = np.empty(n_bootstrap)
    for i in range(n_bootstrap):
        # Assemble a resample of length n by concatenating blocks that start at
        # random indices and wrap around the series (stationary bootstrap).
        pieces = []
        filled = 0
        while filled < n:
            start = rng.integers(0, n)
            idx = (start + np.arange(block)) % n
            pieces.append(returns[idx])
            filled += block
        resample = np.concatenate(pieces)[:n]
        samples[i] = _sharpe_from_returns(resample)

    alpha = (1.0 - confidence) / 2.0
    return {
        "sharpe": round(point, 4),
        "ci_low": round(float(np.quantile(samples, alpha)), 4),
        "ci_high": round(float(np.quantile(samples, 1.0 - alpha)), 4),
        "p_value_positive": round(float(np.mean(samples <= 0)), 4),
        "confidence": confidence,
        "n_bootstrap": n_bootstrap,
    }


# ─── Walk-forward consistency ───

def walk_forward(
    equity_curve: pd.Series,
    trades: list,
    n_windows: int = 5,
) -> dict[str, Any]:
    """Split into N sequential windows; how many are independently profitable?

    Each window's return is normalised to its own start, so a strategy that only
    worked in one lucky stretch shows a low consistency_rate. Trades are bucketed
    by exit_date for a per-window count/win-rate.
    """
    if len(equity_curve) < n_windows * 2:
        return {"error": f"need at least {n_windows * 2} bars for {n_windows} windows"}

    idx = equity_curve.index
    size = len(idx) // n_windows
    windows: list[dict[str, Any]] = []

    for i in range(n_windows):
        start_idx = i * size
        end_idx = (i + 1) * size if i < n_windows - 1 else len(idx)
        eq = equity_curve.iloc[start_idx:end_idx]
        w_start, w_end = idx[start_idx], idx[end_idx - 1]

        ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0) if eq.iloc[0] > 0 else 0.0
        w_returns = eq.pct_change().dropna().to_numpy()
        sharpe = _sharpe_from_returns(w_returns) if w_returns.size > 1 else 0.0

        w_trades = [t for t in trades if w_start <= t.exit_date <= w_end]
        w_wins = sum(1 for t in w_trades if t.return_pct > 0)
        win_rate = w_wins / len(w_trades) if w_trades else 0.0

        windows.append({
            "window": i + 1,
            "start": str(w_start.date()) if hasattr(w_start, "date") else str(w_start),
            "end": str(w_end.date()) if hasattr(w_end, "date") else str(w_end),
            "return": round(ret, 6),
            "sharpe": round(sharpe, 4),
            "max_dd": round(_path_max_dd(w_returns), 6) if w_returns.size else 0.0,
            "trades": len(w_trades),
            "win_rate": round(win_rate, 4),
        })

    rets = [w["return"] for w in windows]
    profitable = sum(1 for r in rets if r > 0)
    return {
        "n_windows": n_windows,
        "windows": windows,
        "profitable_windows": profitable,
        "consistency_rate": round(profitable / n_windows, 4),
        "return_mean": round(float(np.mean(rets)), 6),
        "return_std": round(float(np.std(rets)), 6),
    }


# ─── Aggregator ───

def validate_result(
    result,
    n_simulations: int = 1000,
    n_bootstrap: int = 1000,
    n_windows: int = 5,
    seed: int = 42,
) -> dict[str, Any]:
    """Run all three checks on a BacktestResult (or anything with equity_curve/trades)."""
    return {
        "permutation": permutation_test(result.trades, n_simulations, seed),
        "bootstrap": bootstrap_sharpe_ci(result.equity_curve, n_bootstrap, seed=seed),
        "walk_forward": walk_forward(result.equity_curve, result.trades, n_windows),
    }
