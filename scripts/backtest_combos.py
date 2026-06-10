"""Backtest combinations of the 5 production equity strategies under two
execution models:

  composite — strategies merge into ONE decision per bar (sell > buy > hold),
              trading a single shared position book
  sleeves   — each strategy trades its OWN independent book with an equal
              capital slice; portfolio = average of the sleeve equity curves.
              This mirrors the live pipeline, where every strategy submits its
              own orders and position ownership blocks cross-strategy sells.

Usage:
    python scripts/backtest_combos.py
    python scripts/backtest_combos.py --years 4
    python scripts/backtest_combos.py --equity-symbols QQQ,SPY,NVDA
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
INITIAL_CAPITAL = 100_000.0
DEFAULT_STOP_LOSS = 0.08  # mirrors RiskLimits.stop_loss_pct in the live pipeline

_STOP_LOSS: float | None = DEFAULT_STOP_LOSS  # set from --stop-loss in main()


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


def _factories():
    from trader.strategy.dip_recovery import DipRecovery
    from trader.strategy.donchian_breakout import DonchianBreakout
    from trader.strategy.equity_reversion import EquityBollingerReversion
    from trader.strategy.smash_day import SmashDayB
    from trader.strategy.supertrend import SuperTrend

    return {
        "ST": lambda sym: SuperTrend(symbol=sym),
        "SM": lambda sym: SmashDayB(symbol=sym, long_only=True),
        "BO": lambda sym: EquityBollingerReversion(symbol=sym),
        "DO": lambda sym: DonchianBreakout(symbol=sym),
        "DI": lambda sym: DipRecovery(symbol=sym),
    }


# (label, member keys) — singles first, then the combos under evaluation.
COMBOS: list[tuple[str, list[str]]] = [
    ("SuperTrend", ["ST"]),
    ("SmashDayB", ["SM"]),
    ("BollingerReversion", ["BO"]),
    ("DonchianBreakout", ["DO"]),
    ("DipRecovery", ["DI"]),
    ("ST+Smash", ["ST", "SM"]),
    ("ST+Dip", ["ST", "DI"]),
    ("Donch+Dip", ["DO", "DI"]),
    ("ST+Smash+Dip", ["ST", "SM", "DI"]),
    ("ST+Donch+Dip", ["ST", "DO", "DI"]),
    ("ST+Smash+Boll+Donch (old 4-stack)", ["ST", "SM", "BO", "DO"]),
    ("All 5 (current PROD)", ["ST", "SM", "BO", "DO", "DI"]),
]


def _backtest(strategy, bars: pd.DataFrame):
    from trader.backtest.costs import CostModel
    from trader.backtest.engine import run_backtest

    return run_backtest(
        bars, strategy,
        cost_model=CostModel(slippage_bps=EQUITY_SLIPPAGE_BPS),
        stop_loss_pct=_STOP_LOSS,
    )


def _run_composite(sym: str, keys: list[str], bars: pd.DataFrame, make: dict):
    from trader.backtest.metrics import compute_metrics

    members = [make[k](sym) for k in keys]
    strategy = members[0] if len(members) == 1 else CompositeStrategy(sym, members)
    result = _backtest(strategy, bars)
    return compute_metrics(result.equity_curve, result.trades), len(result.trades)


def _run_sleeves(sym: str, keys: list[str], bars: pd.DataFrame, make: dict):
    """Each member runs an independent backtest; portfolio is the equal-weight
    average of the normalised sleeve curves (each sleeve gets 1/N of capital)."""
    from trader.backtest.metrics import compute_metrics

    curves = []
    trades: list = []
    for k in keys:
        result = _backtest(make[k](sym), bars)
        if result.equity_curve.empty:
            return None
        curves.append(result.equity_curve / result.equity_curve.iloc[0])
        trades.extend(result.trades)
    portfolio = sum(curves) / len(curves) * INITIAL_CAPITAL
    return compute_metrics(portfolio, trades), len(trades)


def _avg(rows: list) -> dict | None:
    valid = [r for r in rows if r is not None]
    if not valid:
        return None
    n = len(valid)
    return {
        "ret": sum(m.total_return for m, _ in valid) / n,
        "sharpe": sum(m.sharpe for m, _ in valid) / n,
        "dd": sum(m.max_drawdown for m, _ in valid) / n,
        "wr": sum(m.win_rate for m, _ in valid) / n,
        "trades": sum(t for _, t in valid),
        "n": n,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Strategy combination backtest")
    parser.add_argument("--equity-symbols", default=None)
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--stop-loss", type=float, default=DEFAULT_STOP_LOSS,
        help="stop-loss fraction matching the live pipeline (0 disables)",
    )
    args = parser.parse_args()

    global _STOP_LOSS
    _STOP_LOSS = args.stop_loss if args.stop_loss > 0 else None

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
    from trader.data.alpaca_bars import get_daily_bars
    from trader.backtest.metrics import compute_metrics

    config = load_config()
    make = _factories()

    _sl = f"{_STOP_LOSS:.0%}" if _STOP_LOSS else "off"
    print(f"\nCombo Backtest  |  {start.date()} → {end.date()}")
    print(f"Symbols: {', '.join(symbols)}  |  Slippage: {EQUITY_SLIPPAGE_BPS}bps  |  Stop-loss: {_sl}")

    results: dict[tuple[str, str], list] = {}
    bh_returns: list[float] = []

    for sym in symbols:
        try:
            bars = get_daily_bars(sym, start=start, end=end, config=config)
        except Exception as exc:
            print(f"  SKIP {sym}: {exc}")
            continue
        if bars.empty or len(bars) < 60:
            print(f"  SKIP {sym}: insufficient data")
            continue

        bh = _backtest(make["DI"](sym), bars).buy_hold_curve
        bh_returns.append(compute_metrics(bh, []).total_return)
        print(f"  {sym}: {len(bars)} bars, B&H {bh_returns[-1]:+.1%}")

        for label, keys in COMBOS:
            results.setdefault((label, "composite"), []).append(
                _run_composite(sym, keys, bars, make)
            )
            if len(keys) > 1:
                results.setdefault((label, "sleeves"), []).append(
                    _run_sleeves(sym, keys, bars, make)
                )

    avg_bh = sum(bh_returns) / len(bh_returns) if bh_returns else 0.0
    header = (
        f"  {'combo':<36} {'model':<10}"
        f" {'return':>8} {'sharpe':>7} {'max_dd':>8} {'win%':>6} {'trades':>6} {'vs B&H':>8}"
    )
    print(f"\n{'='*100}")
    print(f"  SUMMARY — averages across {len(bh_returns)} symbols  (avg B&H {avg_bh:+.1%})")
    print(header)
    print("  " + "-" * 96)
    for label, keys in COMBOS:
        for model in ("composite", "sleeves"):
            rows = results.get((label, model))
            if not rows:
                continue
            a = _avg(rows)
            if a is None:
                continue
            tag = "✓" if a["ret"] > avg_bh else " "
            print(
                f"  {label:<36} {model:<10}"
                f" {a['ret']:>8.1%} {a['sharpe']:>7.2f} {a['dd']:>8.1%}"
                f" {a['wr']:>6.1%} {a['trades']:>6d} {a['ret'] - avg_bh:>+8.1%} {tag}"
            )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
