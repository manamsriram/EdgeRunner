"""Go-live gate: run an out-of-sample backtest for every (symbol, strategy) combination
and check hard thresholds before real money is considered.

Usage:
    python scripts/go_live_gate.py --in-sample-end 2023-12-31
    python scripts/go_live_gate.py --in-sample-end 2023-12-31 --symbols AAPL,MSFT

Exit codes:
    0 — gate passes (≥ 60% of combos meet all thresholds)
    1 — gate fails (< 60% pass, or OOS data too thin)
    2 — configuration error (missing Alpaca keys, bad date, insufficient OOS bars)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, time, timedelta

# Ensure project root is on sys.path so `trader` is importable when the script is
# invoked from any working directory (e.g. `python scripts/go_live_gate.py`).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---- thresholds (change with care; document why if you adjust) ----
MIN_SHARPE = 0.5       # no risk-adjusted edge below this
MAX_DRAWDOWN = -0.25   # catastrophic drawdown floor
MIN_TRADE_COUNT = 5    # too few round-trips to judge
BH_FLOOR_RATIO = 0.8   # OOS return must be > buy-and-hold * this (prevent catastrophic underperform)
PASS_RATIO = 0.60      # fraction of (symbol, strategy) combos that must pass
MIN_OOS_BARS = 60      # fewer bars → metrics are noise


def _check_thresholds(metrics, bh_metrics, trade_count: int) -> tuple[bool, str]:
    """Pure function: returns (passed, reason). Extracted for unit testability."""
    if trade_count < MIN_TRADE_COUNT:
        return False, f"too few trades ({trade_count} < {MIN_TRADE_COUNT})"
    if metrics.sharpe < MIN_SHARPE:
        return False, f"Sharpe {metrics.sharpe:.2f} < {MIN_SHARPE}"
    if metrics.max_drawdown < MAX_DRAWDOWN:
        return False, f"drawdown {metrics.max_drawdown:.1%} < {MAX_DRAWDOWN:.1%}"
    bh_floor = bh_metrics.total_return * BH_FLOOR_RATIO
    if metrics.total_return < bh_floor:
        return False, (
            f"OOS return {metrics.total_return:.1%} < "
            f"{BH_FLOOR_RATIO:.0%} of B&H {bh_metrics.total_return:.1%}"
        )
    return True, "all thresholds met"


def _run_combo(symbol: str, strategy_cls, oos_start: datetime, oos_end: datetime, config):
    from trader.backtest.costs import CostModel
    from trader.backtest.engine import run_backtest
    from trader.backtest.metrics import compute_metrics, format_report
    from trader.data.alpaca_bars import get_daily_bars

    bars = get_daily_bars(symbol, start=oos_start, end=oos_end, config=config)
    if len(bars) < MIN_OOS_BARS:
        return None, f"{symbol}: only {len(bars)} OOS bars (need {MIN_OOS_BARS})"

    strategy = strategy_cls(symbol)
    cost_model = CostModel(slippage_bps=5.0)
    result = run_backtest(bars, strategy, cost_model=cost_model)

    strat_metrics = compute_metrics(result.equity_curve, result.trades)
    bh_metrics = compute_metrics(result.buy_hold_curve, [])
    passed, reason = _check_thresholds(strat_metrics, bh_metrics, len(result.trades))
    report = format_report(result, type(strategy).__name__, symbol)
    return (passed, reason, report, strat_metrics, bh_metrics), None


def main() -> int:
    parser = argparse.ArgumentParser(description="Go-live gate: OOS backtest check")
    parser.add_argument(
        "--in-sample-end",
        required=True,
        help="Last date of in-sample window, e.g. 2023-12-31",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbols override (default: full allowlist)",
    )
    args = parser.parse_args()

    # ---- validate date ----
    try:
        in_sample_end = datetime.strptime(args.in_sample_end, "%Y-%m-%d")
    except ValueError:
        print(f"ERROR: --in-sample-end must be YYYY-MM-DD, got {args.in_sample_end!r}")
        return 2

    today = datetime.combine(date.today(), time())
    oos_start = in_sample_end + timedelta(days=1)
    oos_end = today

    if oos_start >= today:
        print(
            f"ERROR: --in-sample-end {args.in_sample_end} is not in the past; "
            "OOS window would be empty."
        )
        return 2

    # ---- config + auth ----
    try:
        from trader.config import DEFAULT_ALLOWLIST, load_config
        config = load_config()
        config.require_alpaca()
    except Exception as exc:
        print(f"ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env\n  ({exc})")
        return 2

    # ---- symbols ----
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = list(DEFAULT_ALLOWLIST)

    # ---- strategies ----
    from trader.strategy.ma_crossover import MACrossover
    from trader.strategy.momentum_rsi import MomentumRSI
    strategies = [MACrossover, MomentumRSI]

    print(f"Go-Live Gate  |  OOS window: {oos_start.date()} → {oos_end.date()}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Strategies: {', '.join(s.__name__ for s in strategies)}")
    print("=" * 60)

    total = 0
    passed_count = 0
    failures: list[str] = []

    for symbol in symbols:
        for strategy_cls in strategies:
            total += 1
            label = f"{symbol}/{strategy_cls.__name__}"
            try:
                result, err = _run_combo(symbol, strategy_cls, oos_start, oos_end, config)
            except Exception as exc:
                failures.append(f"  FAIL {label}: exception — {exc}")
                print(f"\n[{label}] ERROR: {exc}")
                continue

            if err:
                failures.append(f"  FAIL {label}: {err}")
                print(f"\n[{label}] SKIP: {err}")
                continue

            passed, reason, report, _, _ = result
            print(f"\n{report}")
            if passed:
                passed_count += 1
                print(f"  → PASS: {reason}")
            else:
                failures.append(f"  FAIL {label}: {reason}")
                print(f"  → FAIL: {reason}")

    print("\n" + "=" * 60)
    pass_pct = passed_count / total if total > 0 else 0.0
    print(f"Results: {passed_count}/{total} combos passed ({pass_pct:.0%}; threshold ≥{PASS_RATIO:.0%})")

    if failures:
        print("Failures:")
        for f in failures:
            print(f)

    gate_passed = pass_pct >= PASS_RATIO
    print()
    if gate_passed:
        print("GO-LIVE GATE: PASS")
    else:
        print("GO-LIVE GATE: FAIL")
    return 0 if gate_passed else 1


if __name__ == "__main__":
    sys.exit(main())
