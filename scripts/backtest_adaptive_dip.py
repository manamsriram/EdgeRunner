"""Validate regime-adaptive DipRecovery params against the fixed baseline.

Protocol (anti-overfit):
  1. Split the full period in half: in-sample (first half) and out-of-sample
     (second half).
  2. Grid-search the per-regime dip_pct table on the IN-SAMPLE window only and
     pick the best table by average total return across symbols.
  3. Run the winning table ONCE on the out-of-sample window against the fixed
     baseline. Deploy only if it wins there.

Each window is backtested with a leading warmup (regime classification needs
~273 trading bars; the ATH anchor also benefits), then the equity curve is
sliced to the evaluation window so warmup trading does not pollute the score.
DipRecovery runs without a stop-loss, matching its live exemption.

Usage:
    python scripts/backtest_adaptive_dip.py
    python scripts/backtest_adaptive_dip.py --years 4 --equity-symbols QQQ,SPY
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
# Calendar days fetched before each window so regime classification (273 trading
# bars) is warm by the time the evaluation window starts.
WARMUP_CALENDAR_DAYS = 420
EXPANSION_PCT = 0.05  # exit param held at the production value throughout

CALM_DIP_GRID = [0.06, 0.08, 0.10]
NORMAL_DIP_GRID = [0.08, 0.10, 0.12]
STRESSED_DIP_GRID = [0.10, 0.15, 0.20]

FIXED_DIP = 0.10  # current production DipRecovery


def _make_dip(regime_table: dict | None):
    from trader.strategy.dip_recovery import DipRecovery

    def factory(sym: str):
        return DipRecovery(symbol=sym, dip_pct=FIXED_DIP,
                           expansion_pct=EXPANSION_PCT, regime_params=regime_table)
    return factory


def _window_metrics(factory, bars: pd.DataFrame, win_start, win_end):
    """Backtest from the first available bar through win_end, then score only the
    [win_start, win_end] slice of the equity curve."""
    from trader.backtest.costs import CostModel
    from trader.backtest.engine import run_backtest
    from trader.backtest.metrics import compute_metrics

    sub = bars.loc[bars.index <= win_end]
    if len(sub) < 60:
        return None
    result = run_backtest(
        sub, factory(str(bars.attrs.get("symbol", "SYM"))),
        initial_cash=INITIAL_CAPITAL,
        cost_model=CostModel(slippage_bps=EQUITY_SLIPPAGE_BPS),
        stop_loss_pct=None,  # DipRecovery is stop-exempt in the live pipeline
    )
    curve = result.equity_curve.loc[result.equity_curve.index >= win_start]
    if len(curve) < 2:
        return None
    curve = curve / curve.iloc[0] * INITIAL_CAPITAL
    trades = [t for t in result.trades if t.exit_date >= win_start]
    return compute_metrics(curve, trades)


def _buy_hold_return(bars: pd.DataFrame, win_start, win_end) -> float | None:
    closes = bars.loc[(bars.index >= win_start) & (bars.index <= win_end), "close"]
    if len(closes) < 2:
        return None
    return float(closes.iloc[-1] / closes.iloc[0] - 1.0)


def _avg_return(scores: list) -> float | None:
    valid = [m for m in scores if m is not None]
    if not valid:
        return None
    return sum(m.total_return for m in valid) / len(valid)


def main() -> int:
    parser = argparse.ArgumentParser(description="Adaptive DipRecovery validation")
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

    print(f"\nAdaptive DipRecovery Validation")
    print(f"  In-sample:     {start.date()} → {mid.date()}")
    print(f"  Out-of-sample: {mid.date()} → {end.date()}")
    print(f"  Symbols: {', '.join(symbols)}  |  Slippage: {EQUITY_SLIPPAGE_BPS}bps"
          f"  |  Stop-loss: off (DipRecovery exempt in prod)")

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

    # ---- In-sample grid search ----
    tables = [
        {"calm": (c, EXPANSION_PCT), "normal": (n, EXPANSION_PCT),
         "stressed": (s, EXPANSION_PCT)}
        for c, n, s in itertools.product(
            CALM_DIP_GRID, NORMAL_DIP_GRID, STRESSED_DIP_GRID)
    ]
    fixed_factory = _make_dip(None)
    fixed_is = _avg_return(
        [_window_metrics(fixed_factory, b, win_start, win_mid)
         for b in all_bars.values()]
    )
    print(f"\n  IN-SAMPLE  fixed dip={FIXED_DIP:.0%}: {fixed_is:+.1%}")

    best_table, best_is = None, None
    for table in tables:
        score = _avg_return(
            [_window_metrics(_make_dip(table), b, win_start, win_mid)
             for b in all_bars.values()]
        )
        if score is not None and (best_is is None or score > best_is):
            best_table, best_is = table, score
    label = {r: f"{t[0]:.0%}" for r, t in best_table.items()}
    print(f"  IN-SAMPLE  best table {label}: {best_is:+.1%}"
          f"  (edge vs fixed {best_is - fixed_is:+.1%})")

    # ---- Out-of-sample verification ----
    fixed_oos = [_window_metrics(fixed_factory, b, win_mid, win_end)
                 for b in all_bars.values()]
    adaptive_oos = [_window_metrics(_make_dip(best_table), b, win_mid, win_end)
                    for b in all_bars.values()]
    bh_oos = [_buy_hold_return(b, win_mid, win_end) for b in all_bars.values()]
    bh_avg = sum(r for r in bh_oos if r is not None) / max(
        1, sum(1 for r in bh_oos if r is not None))

    def _fmt(scores: list) -> str:
        valid = [m for m in scores if m is not None]
        ret = sum(m.total_return for m in valid) / len(valid)
        dd = sum(m.max_drawdown for m in valid) / len(valid)
        sharpe = sum(m.sharpe for m in valid) / len(valid)
        return f"return {ret:+8.1%}  sharpe {sharpe:5.2f}  max_dd {dd:8.1%}"

    print(f"\n  OUT-OF-SAMPLE ({win_mid.date()} → {win_end.date()},"
          f" avg B&H {bh_avg:+.1%})")
    print(f"    fixed    dip={FIXED_DIP:.0%}:        {_fmt(fixed_oos)}")
    print(f"    adaptive {label}: {_fmt(adaptive_oos)}")

    fixed_ret = _avg_return(fixed_oos)
    adaptive_ret = _avg_return(adaptive_oos)
    verdict = "DEPLOY adaptive" if adaptive_ret > fixed_ret else "KEEP fixed"
    print(f"\n  VERDICT: {verdict}"
          f"  (OOS adaptive {adaptive_ret:+.1%} vs fixed {fixed_ret:+.1%})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
