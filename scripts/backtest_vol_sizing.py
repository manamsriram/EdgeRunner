"""Validate vol-targeted position sizing on the production ST+Dip sleeves stack.

Protocol (anti-overfit, same as backtest_adaptive_dip.py):
  1. Half/half in-sample / out-of-sample split with a 420-day warmup per window.
  2. Grid-search (target_vol × application) on the IN-SAMPLE window only,
     selecting by SHARPE — vol targeting trades raw return for risk-adjusted
     return, so judging on total return would always favor full size.
  3. Run the winner ONCE out-of-sample against the unsized baseline.

Application axis: scale ST entries only, or both ST and DipRecovery. Lesson
from the stop-loss work: DipRecovery's edge IS taking high-vol dip risk, so a
uniform overlay may hurt it — the grid lets the data decide.

Stops mirror live: ST carries the 8% stop, DipRecovery is exempt.

Usage:
    python scripts/backtest_vol_sizing.py
    python scripts/backtest_vol_sizing.py --years 4 --equity-symbols QQQ,SPY
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DEFAULT_EQUITY_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "SPY", "QQQ", "TSLA",
]
DEFAULT_YEARS = 4
EQUITY_SLIPPAGE_BPS = 5.0
INITIAL_CAPITAL = 100_000.0
WARMUP_CALENDAR_DAYS = 420
ST_STOP_LOSS = 0.08      # live pipeline stop applies to SuperTrend
VOL_FLOOR = 0.25

TARGET_VOL_GRID = [0.10, 0.15, 0.20, 0.25, 0.30]
APPLY_GRID = ["ST", "ST+DI"]  # which sleeves get vol-sized entries

# Return give-up guard: a Sharpe win that costs more than a third of the
# baseline's OOS return is flagged for human review instead of auto-deploy.
RETURN_GIVEUP_LIMIT = 1.0 / 3.0


def _sizer(target_vol: float):
    from trader.risk.vol_sizing import vol_scale
    return lambda visible: vol_scale(visible, target_vol=target_vol, floor=VOL_FLOOR)


def _sleeve_metrics(bars: pd.DataFrame, win_start, win_end,
                    target_vol: float | None, apply: str):
    """ST+Dip sleeves portfolio over one window: independent backtests, sliced
    normalized curves averaged into one portfolio curve."""
    from trader.backtest.costs import CostModel
    from trader.backtest.engine import run_backtest
    from trader.backtest.metrics import compute_metrics
    from trader.strategy.dip_recovery import DipRecovery
    from trader.strategy.supertrend import SuperTrend

    sym = str(bars.attrs.get("symbol", "SYM"))
    sub = bars.loc[bars.index <= win_end]
    if len(sub) < 60:
        return None

    sizer = _sizer(target_vol) if target_vol is not None else None
    sleeves = [
        (SuperTrend(symbol=sym), ST_STOP_LOSS, sizer),
        (DipRecovery(symbol=sym), None, sizer if apply == "ST+DI" else None),
    ]

    curves = []
    trades: list = []
    for strategy, stop, fraction in sleeves:
        result = run_backtest(
            sub, strategy,
            initial_cash=INITIAL_CAPITAL,
            cost_model=CostModel(slippage_bps=EQUITY_SLIPPAGE_BPS),
            stop_loss_pct=stop,
            entry_fraction=fraction,
        )
        curve = result.equity_curve.loc[result.equity_curve.index >= win_start]
        if len(curve) < 2:
            return None
        curves.append(curve / curve.iloc[0])
        trades.extend(t for t in result.trades if t.exit_date >= win_start)
    portfolio = sum(curves) / len(curves) * INITIAL_CAPITAL
    return compute_metrics(portfolio, trades)


def _avg(scores: list) -> dict | None:
    valid = [m for m in scores if m is not None]
    if not valid:
        return None
    n = len(valid)
    return {
        "ret": sum(m.total_return for m in valid) / n,
        "sharpe": sum(m.sharpe for m in valid) / n,
        "dd": sum(m.max_drawdown for m in valid) / n,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Vol-targeted sizing validation")
    parser.add_argument("--equity-symbols", default=None)
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.utcnow()
    start = end - timedelta(days=365 * args.years)
    mid = start + (end - start) / 2
    fetch_start = start - timedelta(days=WARMUP_CALENDAR_DAYS)
    symbols = (
        [s.strip().upper() for s in args.equity_symbols.split(",")]
        if args.equity_symbols else DEFAULT_EQUITY_SYMBOLS
    )

    from trader.config import load_config
    from trader.data.alpaca_bars import get_daily_bars

    config = load_config()

    print(f"\nVol-Targeted Sizing Validation  (ST+Dip sleeves, ST stop {ST_STOP_LOSS:.0%},"
          f" Dip exempt, floor {VOL_FLOOR:.0%})")
    print(f"  In-sample:     {start.date()} → {mid.date()}")
    print(f"  Out-of-sample: {mid.date()} → {end.date()}")
    print(f"  Symbols: {', '.join(symbols)}")

    all_bars: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            bars = get_daily_bars(sym, start=fetch_start, end=end, config=config)
        except Exception as exc:
            print(f"  SKIP {sym}: {exc}")
            continue
        if bars.empty or len(bars) < 400:
            print(f"  SKIP {sym}: insufficient data")
            continue
        bars.attrs["symbol"] = sym
        all_bars[sym] = bars
    if not all_bars:
        print("No usable symbols.")
        return 1

    win_start, win_mid, win_end = (
        pd.Timestamp(start), pd.Timestamp(mid), pd.Timestamp(end)
    )

    def evaluate(target_vol, apply, lo, hi):
        return _avg([_sleeve_metrics(b, lo, hi, target_vol, apply)
                     for b in all_bars.values()])

    # ---- In-sample grid search (Sharpe-primary) ----
    baseline_is = evaluate(None, "ST", win_start, win_mid)
    print(f"\n  IN-SAMPLE  baseline (unsized): "
          f"sharpe {baseline_is['sharpe']:.2f}  return {baseline_is['ret']:+.1%}"
          f"  max_dd {baseline_is['dd']:+.1%}")

    best_cfg, best_is = None, None
    for tv, apply in itertools.product(TARGET_VOL_GRID, APPLY_GRID):
        a = evaluate(tv, apply, win_start, win_mid)
        if a is not None and (best_is is None or a["sharpe"] > best_is["sharpe"]):
            best_cfg, best_is = (tv, apply), a
    tv, apply = best_cfg
    print(f"  IN-SAMPLE  best: target_vol {tv:.0%} on {apply}: "
          f"sharpe {best_is['sharpe']:.2f}  return {best_is['ret']:+.1%}"
          f"  max_dd {best_is['dd']:+.1%}")

    # ---- Out-of-sample verification ----
    baseline_oos = evaluate(None, "ST", win_mid, win_end)
    sized_oos = evaluate(tv, apply, win_mid, win_end)

    print(f"\n  OUT-OF-SAMPLE ({win_mid.date()} → {win_end.date()})")
    print(f"    baseline (unsized):          sharpe {baseline_oos['sharpe']:.2f}"
          f"  return {baseline_oos['ret']:+.1%}  max_dd {baseline_oos['dd']:+.1%}")
    print(f"    vol-sized ({tv:.0%} on {apply}):    sharpe {sized_oos['sharpe']:.2f}"
          f"  return {sized_oos['ret']:+.1%}  max_dd {sized_oos['dd']:+.1%}")

    sharpe_win = sized_oos["sharpe"] > baseline_oos["sharpe"]
    giveup = (
        baseline_oos["ret"] > 0
        and sized_oos["ret"] < baseline_oos["ret"] * (1.0 - RETURN_GIVEUP_LIMIT)
    )
    if sharpe_win and not giveup:
        verdict = f"DEPLOY vol sizing (target_vol {tv:.0%} on {apply})"
    elif sharpe_win:
        verdict = "SHARPE WIN but return give-up exceeds limit — human review"
    else:
        verdict = "KEEP flat sizing"
    print(f"\n  VERDICT: {verdict}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
