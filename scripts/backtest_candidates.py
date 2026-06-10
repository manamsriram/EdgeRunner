"""Backtest the new candidate strategies (DipRecovery, HAPullback) against the
production equity stack.

Sections per symbol:
  1. Individual strategies — production four + the two candidates
  2. Production combo (SuperTrend + SmashDayB + EquityBollingerReversion + DonchianBreakout)
  3. Candidate combos — production + one candidate, and production + both

Usage:
    python scripts/backtest_candidates.py
    python scripts/backtest_candidates.py --years 4
    python scripts/backtest_candidates.py --equity-symbols QQQ,SPY,NVDA
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DEFAULT_EQUITY_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "SPY", "QQQ", "TSLA"]
DEFAULT_YEARS = 2
EQUITY_SLIPPAGE_BPS = 5.0


class CompositeStrategy:
    """Runs multiple strategies and merges signals: sell > buy > hold."""

    def __init__(self, symbol: str, strategies: list) -> None:
        self.symbol = symbol
        self._strategies = strategies

    def generate(self, bars: pd.DataFrame, asof: pd.Timestamp):
        from trader.strategy.base import Signal
        signals = [s.generate(bars, asof) for s in self._strategies]
        sells = [s for s in signals if s.side == "sell"]
        buys = [s for s in signals if s.side == "buy"]
        if sells:
            best = max(sells, key=lambda s: s.strength)
            return Signal(self.symbol, "sell", best.strength, best.reason)
        if buys:
            best = max(buys, key=lambda s: s.strength)
            return Signal(self.symbol, "buy", best.strength, best.reason)
        return Signal(self.symbol, "hold", 0.0, "all strategies hold")


def _run(strategy, bars: pd.DataFrame):
    from trader.backtest.costs import CostModel
    from trader.backtest.engine import run_backtest
    from trader.backtest.metrics import compute_metrics

    if len(bars) < 60:
        return None
    result = run_backtest(bars, strategy, cost_model=CostModel(slippage_bps=EQUITY_SLIPPAGE_BPS))
    return {
        "strat": compute_metrics(result.equity_curve, result.trades),
        "bh": compute_metrics(result.buy_hold_curve, []),
        "trades": len(result.trades),
    }


def _row(label: str, r) -> str:
    if r is None:
        return f"  {label:<38}  {'SKIP':>8}"
    s, bh, t = r["strat"], r["bh"], r["trades"]
    tag = "✓" if s.total_return > bh.total_return else "✗"
    return (
        f"  {label:<38}"
        f"  {s.total_return:>8.1%}"
        f"  {s.sharpe:>7.2f}"
        f"  {s.max_drawdown:>9.1%}"
        f"  {s.win_rate:>7.1%}"
        f"  {t:>6d}"
        f"  {s.total_return - bh.total_return:>+9.1%} {tag}"
    )


HEADER = (
    f"  {'label':<38}"
    f"  {'return':>8}"
    f"  {'sharpe':>7}"
    f"  {'max_dd':>9}"
    f"  {'win%':>7}"
    f"  {'trades':>6}"
    f"  {'vs B&H':>9}"
)
SEP = "  " + "-" * 105


def _section(title: str) -> None:
    print(f"\n{'='*109}")
    print(f"  {title}")
    print(HEADER)
    print(SEP)


def _summarise(all_results: dict) -> None:
    print(f"\n{'='*109}")
    print("  SUMMARY — averages across all symbols per label")
    print(HEADER)
    print(SEP)
    for label, rows in all_results.items():
        valid = [r for r in rows if r is not None]
        if not valid:
            print(f"  {label:<38}  no data")
            continue
        avg_ret = sum(r["strat"].total_return for r in valid) / len(valid)
        avg_sh = sum(r["strat"].sharpe for r in valid) / len(valid)
        avg_dd = sum(r["strat"].max_drawdown for r in valid) / len(valid)
        avg_wr = sum(r["strat"].win_rate for r in valid) / len(valid)
        total_t = sum(r["trades"] for r in valid)
        avg_bh = sum(r["bh"].total_return for r in valid) / len(valid)
        tag = "✓" if avg_ret > avg_bh else "✗"
        print(
            f"  {label:<38}"
            f"  {avg_ret:>8.1%}"
            f"  {avg_sh:>7.2f}"
            f"  {avg_dd:>9.1%}"
            f"  {avg_wr:>7.1%}"
            f"  {total_t:>6d}"
            f"  {avg_ret - avg_bh:>+9.1%} {tag}"
        )
    print(f"  {'  avg buy & hold':<38}  {sum(r['bh'].total_return for rows in all_results.values() for r in rows if r) / max(sum(1 for rows in all_results.values() for r in rows if r), 1):>8.1%}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Candidate strategy backtest")
    parser.add_argument("--equity-symbols", default=None)
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.utcnow()
    start = (
        datetime.strptime(args.start, "%Y-%m-%d")
        if args.start
        else end - timedelta(days=365 * args.years)
    )
    symbols = (
        [s.strip().upper() for s in args.equity_symbols.split(",")]
        if args.equity_symbols else DEFAULT_EQUITY_SYMBOLS
    )

    from trader.config import load_config
    config = load_config()

    from trader.data.alpaca_bars import get_daily_bars
    from trader.strategy.dip_recovery import DipRecovery
    from trader.strategy.donchian_breakout import DonchianBreakout
    from trader.strategy.equity_reversion import EquityBollingerReversion
    from trader.strategy.ha_pullback import HAPullback
    from trader.strategy.smash_day import SmashDayB
    from trader.strategy.supertrend import SuperTrend

    def prod(sym):
        return [
            SuperTrend(symbol=sym),
            SmashDayB(symbol=sym, long_only=True),
            EquityBollingerReversion(symbol=sym),
            DonchianBreakout(symbol=sym),
        ]

    print(f"\nCandidate Backtest  |  {start.date()} → {end.date()}")
    print(f"Symbols: {', '.join(symbols)}  |  Slippage: {EQUITY_SLIPPAGE_BPS}bps")

    all_results: dict[str, list] = {}

    for sym in symbols:
        try:
            bars = get_daily_bars(sym, start=start, end=end, config=config)
        except Exception as exc:
            print(f"  SKIP {sym}: {exc}")
            continue
        if bars.empty:
            print(f"  SKIP {sym}: no data")
            continue

        from trader.backtest.costs import CostModel
        from trader.backtest.engine import run_backtest
        from trader.backtest.metrics import compute_metrics
        bh_result = run_backtest(bars, DipRecovery(symbol=sym), cost_model=CostModel(slippage_bps=EQUITY_SLIPPAGE_BPS))
        bh_metrics = compute_metrics(bh_result.buy_hold_curve, [])

        _section(f"EQUITY — {sym}  (B&H: {bh_metrics.total_return:.1%})")

        combos = [
            ("SuperTrend", SuperTrend(symbol=sym)),
            ("SmashDayB", SmashDayB(symbol=sym, long_only=True)),
            ("EquityBollingerReversion", EquityBollingerReversion(symbol=sym)),
            ("DonchianBreakout", DonchianBreakout(symbol=sym)),
            ("NEW: DipRecovery", DipRecovery(symbol=sym)),
            ("NEW: HAPullback", HAPullback(symbol=sym)),
        ]
        for label, strat in combos:
            r = _run(strat, bars)
            all_results.setdefault(label, []).append(r)
            print(_row(label, r))

        print(SEP)

        stacks = [
            ("PROD: ST+Smash+Boll+Donch", prod(sym)),
            ("PROD + DipRecovery", prod(sym) + [DipRecovery(symbol=sym)]),
            ("PROD + HAPullback", prod(sym) + [HAPullback(symbol=sym)]),
            ("PROD + both", prod(sym) + [DipRecovery(symbol=sym), HAPullback(symbol=sym)]),
        ]
        for label, strats in stacks:
            r = _run(CompositeStrategy(sym, strats), bars)
            all_results.setdefault(label, []).append(r)
            print(_row(label, r))

    _summarise(all_results)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
