"""Live paper trading performance report — CLI entry point.

Pulls from Alpaca (equity curve + fills) and Supabase (signal counts) to compute
performance metrics and print a PASS/FAIL go-live verdict.

Usage:
    python scripts/performance_tracker.py

Exit codes:
    0 — PASS: all thresholds met
    1 — FAIL or INSUFFICIENT_DATA: below threshold or no data yet
    2 — config error (missing Alpaca keys)
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _check_mark(passing: bool) -> str:
    return "✓" if passing else "✗"


def _fmt_pct(v: float) -> str:
    return f"{v:+.1%}"


def main() -> int:
    try:
        from trader.config import load_config
        config = load_config()
        config.require_alpaca()
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 2

    try:
        from trader.execution.broker import AlpacaBroker
        broker = AlpacaBroker(config)

        from trader.portfolio.postgres_repo import PostgresRepository
        repo = PostgresRepository(config.database_url)

        from trader.performance.metrics import (
            MAX_DRAWDOWN, MIN_DAYS, MIN_PROFIT_FACTOR, MIN_SHARPE,
            MIN_TRADES, MIN_WIN_RATE, compute_live_metrics,
        )
        m = compute_live_metrics(config, broker, repo)
    except Exception as exc:
        print(f"ERROR: could not compute metrics — {exc}")
        return 2

    sep = "=" * 60
    print(sep)
    print("Live Paper Trading — Performance Report")
    print(sep)

    if m.verdict == "INSUFFICIENT_DATA":
        print("INSUFFICIENT DATA — run the scheduler in auto mode to populate.")
        return 1

    def _row(label, value, threshold_str, passing):
        mark = _check_mark(passing)
        print(f"{label:<22}: {value:>10}  (threshold {threshold_str})  {mark}")

    _row("Days active", m.days_active, f"≥{MIN_DAYS}", m.days_active >= MIN_DAYS)
    _row("Trades (round-trips)", m.trade_count, f"≥{MIN_TRADES}", m.trade_count >= MIN_TRADES)
    _row("Sharpe", f"{m.sharpe:.2f}", f"≥{MIN_SHARPE}", m.sharpe >= MIN_SHARPE)
    _row(
        "Max drawdown",
        f"{m.max_drawdown:.1%}",
        f"≤{abs(MAX_DRAWDOWN):.0%}",
        m.max_drawdown >= MAX_DRAWDOWN,
    )
    _row("Win rate", f"{m.win_rate:.1%}", f"≥{MIN_WIN_RATE:.0%}", m.win_rate >= MIN_WIN_RATE)
    pf_display = "∞" if math.isinf(m.profit_factor) else f"{m.profit_factor:.2f}"
    pf_pass = math.isinf(m.profit_factor) or m.profit_factor >= MIN_PROFIT_FACTOR
    _row("Profit factor", pf_display, f"≥{MIN_PROFIT_FACTOR}", pf_pass)

    print()
    print("Benchmark comparison  (informational — not gated)")
    spy = _fmt_pct(m.benchmark_spy_return) if m.benchmark_spy_return is not None else "unavailable"
    btc = _fmt_pct(m.benchmark_btc_return) if m.benchmark_btc_return is not None else "unavailable"
    print(f"  Portfolio  : {_fmt_pct(m.total_return)}")
    print(f"  SPY        : {spy}")
    print(f"  BTC/USD    : {btc}")

    if m.strategy_signals:
        print()
        print("Strategy signals (V1 — counts only, not P&L)")
        for strategy, count in sorted(m.strategy_signals.items(), key=lambda x: -x[1]):
            print(f"  {strategy:<22}: {count} signals")

    if m.failing_checks:
        print()
        print("Failing checks:")
        for reason in m.failing_checks:
            print(f"  • {reason}")

    print()
    print(sep)
    print(f"GO-LIVE VERDICT: {m.verdict}")
    print(sep)

    return 0 if m.verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
