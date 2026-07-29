"""Test a market-wide stress gate on DipRecovery: block new buys whenever SPY is
below its own 50-day SMA (broad risk-off), vs. the current per-symbol-only gating.

Rationale: prod DipRecovery fired 15 uncorrelated-looking dip buys on 2026-07-29
during what was actually a single broad-market selloff — the strategy has no
cross-symbol/market-wide view, only each symbol's own ATH drawdown. This is a
single fixed-rule test (no grid search — one gate definition, no overfit risk)
against the full backtest window, scored against the current fixed-dip_pct=0.20
baseline.

Usage:
    python scripts/backtest_dip_market_gate.py
    python scripts/backtest_dip_market_gate.py --years 4 --equity-symbols QQQ,SPY
"""
from __future__ import annotations

import argparse
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
MARKET_SYMBOL = "SPY"
MARKET_SMA_WINDOW = 50
WARMUP_CALENDAR_DAYS = 120  # enough for a 50d SMA to warm up


def _market_ok_series(spy_bars: pd.DataFrame) -> pd.Series:
    """True on days SPY closes above its own 50-day SMA (risk-on)."""
    sma = spy_bars["close"].rolling(MARKET_SMA_WINDOW).mean()
    return spy_bars["close"] > sma


class GatedDipRecovery:
    """Wraps a DipRecovery instance: buy signals are downgraded to hold on any
    date the market-wide gate is closed. Sells always pass through untouched."""

    def __init__(self, inner, market_ok: pd.Series):
        self._inner = inner
        self._market_ok = market_ok
        self.symbol = inner.symbol

    def generate(self, bars: pd.DataFrame, asof: pd.Timestamp):
        signal = self._inner.generate(bars, asof)
        if signal.side == "buy" and not bool(self._market_ok.get(asof, True)):
            from trader.strategy.base import Signal
            return Signal(signal.symbol, "hold", 0.0,
                          f"buy suppressed: market gate closed (SPY < {MARKET_SMA_WINDOW}d SMA)")
        return signal


def main() -> int:
    parser = argparse.ArgumentParser(description="DipRecovery market-stress gate test")
    parser.add_argument("--equity-symbols", default=None)
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.utcnow()
    start = end - timedelta(days=365 * args.years)
    fetch_start = start - timedelta(days=WARMUP_CALENDAR_DAYS)
    symbols = (
        [s.strip().upper() for s in args.equity_symbols.split(",")]
        if args.equity_symbols else DEFAULT_EQUITY_SYMBOLS
    )

    from trader.backtest.costs import CostModel
    from trader.backtest.engine import run_backtest
    from trader.backtest.metrics import compute_metrics
    from trader.config import load_config
    from trader.data.alpaca_bars import get_daily_bars
    from trader.strategy.dip_recovery import DipRecovery

    config = load_config()

    print(f"\nDipRecovery Market-Stress Gate Test")
    print(f"  Window: {start.date()} -> {end.date()}  |  Symbols: {', '.join(symbols)}")
    print(f"  Gate: block new buys when {MARKET_SYMBOL} closes below its "
          f"{MARKET_SMA_WINDOW}d SMA\n")

    spy_bars = get_daily_bars(MARKET_SYMBOL, start=fetch_start, end=end, config=config)
    market_ok = _market_ok_series(spy_bars)

    cost_model = CostModel(slippage_bps=EQUITY_SLIPPAGE_BPS)
    baseline_scores, gated_scores = [], []

    for sym in symbols:
        try:
            bars = get_daily_bars(sym, start=fetch_start, end=end, config=config)
        except Exception as exc:
            print(f"  SKIP {sym}: {exc}")
            continue
        if bars.empty or len(bars) < 60:
            print(f"  SKIP {sym}: insufficient data")
            continue

        baseline = run_backtest(
            bars, DipRecovery(symbol=sym, dip_pct=0.20, expansion_pct=0.05),
            initial_cash=INITIAL_CAPITAL, cost_model=cost_model, stop_loss_pct=None,
        )
        gated = run_backtest(
            bars, GatedDipRecovery(
                DipRecovery(symbol=sym, dip_pct=0.20, expansion_pct=0.05), market_ok),
            initial_cash=INITIAL_CAPITAL, cost_model=cost_model, stop_loss_pct=None,
        )
        bm = compute_metrics(baseline.equity_curve, baseline.trades)
        gm = compute_metrics(gated.equity_curve, gated.trades)
        print(f"  {sym:6s} baseline: return {bm.total_return:+7.1%} sharpe {bm.sharpe:5.2f}"
              f" max_dd {bm.max_drawdown:7.1%} trades {len(baseline.trades):3d}"
              f"   |   gated: return {gm.total_return:+7.1%} sharpe {gm.sharpe:5.2f}"
              f" max_dd {gm.max_drawdown:7.1%} trades {len(gated.trades):3d}")
        baseline_scores.append(bm)
        gated_scores.append(gm)

    if not baseline_scores:
        print("No usable symbols.")
        return 1

    def _avg(attr, scores):
        return sum(getattr(m, attr) for m in scores) / len(scores)

    print(f"\n  AVG baseline: return {_avg('total_return', baseline_scores):+.1%}"
          f"  sharpe {_avg('sharpe', baseline_scores):.2f}"
          f"  max_dd {_avg('max_drawdown', baseline_scores):.1%}")
    print(f"  AVG gated:    return {_avg('total_return', gated_scores):+.1%}"
          f"  sharpe {_avg('sharpe', gated_scores):.2f}"
          f"  max_dd {_avg('max_drawdown', gated_scores):.1%}")
    verdict = "gate HELPS" if _avg("sharpe", gated_scores) > _avg("sharpe", baseline_scores) else "gate does NOT help"
    print(f"\n  VERDICT: {verdict}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
