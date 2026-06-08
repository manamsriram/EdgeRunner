"""Comprehensive backtest: equity + crypto, individual strategies + production combos + candidate additions.

Sections:
  1. Individual strategies — shows each strategy's standalone edge
  2. Production combos — mirrors the live scheduler stacks exactly
  3. Candidate additions — production combo + one new strategy added

Composite strategy logic (mirrors live pipeline):
  - Any sell signal → sell (risk-priority)
  - Any buy signal and no sell → buy
  - All hold → hold
  Strength = max strength across all signals.

Usage:
    python scripts/backtest_full.py
    python scripts/backtest_full.py --years 1
    python scripts/backtest_full.py --equity-symbols AAPL,MSFT --crypto-symbols SOL/USD,XRP/USD
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DEFAULT_EQUITY_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "SPY"]
DEFAULT_CRYPTO_SYMBOLS = ["SOL/USD", "LINK/USD", "XRP/USD", "BAT/USD", "FIL/USD"]
DEFAULT_YEARS = 2

EQUITY_SLIPPAGE_BPS = 5.0
CRYPTO_SLIPPAGE_BPS = 10.0


# ---- Composite strategy -------------------------------------------------------

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
            reasons = "; ".join(f"{type(strat).__name__}:{sig.reason}"
                                for strat, sig in zip(self._strategies, signals)
                                if sig.side == "sell")
            return Signal(self.symbol, "sell", best.strength, reasons)
        if buys:
            best = max(buys, key=lambda s: s.strength)
            reasons = "; ".join(f"{type(strat).__name__}:{sig.reason}"
                                for strat, sig in zip(self._strategies, signals)
                                if sig.side == "buy")
            return Signal(self.symbol, "buy", best.strength, reasons)
        return Signal(self.symbol, "hold", 0.0, "all strategies hold")


# ---- helpers ------------------------------------------------------------------

def _run(symbol: str, strategy, bars: pd.DataFrame, slippage_bps: float):
    from trader.backtest.costs import CostModel
    from trader.backtest.engine import run_backtest
    from trader.backtest.metrics import compute_metrics

    if len(bars) < 60:
        return None
    cost_model = CostModel(slippage_bps=slippage_bps)
    result = run_backtest(bars, strategy, cost_model=cost_model)
    strat = compute_metrics(result.equity_curve, result.trades)
    bh = compute_metrics(result.buy_hold_curve, [])
    return {"strat": strat, "bh": bh, "trades": len(result.trades)}


def _fetch_equity(symbol: str, start: datetime, end: datetime, config) -> pd.DataFrame:
    from trader.data.alpaca_bars import get_daily_bars
    return get_daily_bars(symbol, start=start, end=end, config=config)


def _fetch_crypto(symbol: str, start: datetime, end: datetime, config) -> pd.DataFrame:
    from trader.data.crypto_bars import get_crypto_bars
    return get_crypto_bars(symbol, start=start, end=end, config=config)


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


def _bh_row(bh_metrics) -> str:
    return (
        f"  {'  buy & hold':<38}"
        f"  {bh_metrics.total_return:>8.1%}"
        f"  {bh_metrics.sharpe:>7.2f}"
        f"  {bh_metrics.max_drawdown:>9.1%}"
        f"  {'n/a':>7}"
        f"  {'0':>6}"
        f"  {'baseline':>9}"
    )


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


# ---- main ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Full equity + crypto backtest")
    parser.add_argument("--equity-symbols", default=None)
    parser.add_argument("--crypto-symbols", default=None)
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
    equity_syms = (
        [s.strip().upper() for s in args.equity_symbols.split(",")]
        if args.equity_symbols else DEFAULT_EQUITY_SYMBOLS
    )
    crypto_syms = (
        [s.strip() for s in args.crypto_symbols.split(",")]
        if args.crypto_symbols else DEFAULT_CRYPTO_SYMBOLS
    )

    try:
        from trader.config import load_config
        config = load_config()
    except Exception as exc:
        print(f"ERROR loading config: {exc}")
        return 2

    # ---- strategy imports ----
    from trader.strategy.crypto_trend import CryptoEMACrossover
    from trader.strategy.gap_pattern import GapPatternA
    from trader.strategy.ma_crossover import MACrossover
    from trader.strategy.smash_day import SmashDayB

    print(f"\nFull Backtest  |  {start.date()} → {end.date()}")
    print(f"Equity: {', '.join(equity_syms)}")
    print(f"Crypto: {', '.join(crypto_syms)}")
    print(f"Slippage: equity={EQUITY_SLIPPAGE_BPS}bps  crypto={CRYPTO_SLIPPAGE_BPS}bps")

    # ===== EQUITY =====

    equity_all: dict[str, list] = {}

    for sym in equity_syms:
        bars = _fetch_equity(sym, start, end, config)
        if bars.empty:
            print(f"  SKIP {sym}: no data")
            continue

        # buy & hold reference
        from trader.backtest.metrics import compute_metrics
        from trader.backtest.costs import CostModel
        from trader.backtest.engine import run_backtest
        bh_result = run_backtest(bars, MACrossover(symbol=sym), cost_model=CostModel(slippage_bps=EQUITY_SLIPPAGE_BPS))
        bh_metrics = compute_metrics(bh_result.buy_hold_curve, [])

        _section(f"EQUITY — {sym}  (B&H: {bh_metrics.total_return:.1%})")

        # individual
        combos = [
            ("MACrossover", MACrossover(symbol=sym)),
            ("SmashDayB", SmashDayB(symbol=sym, long_only=True)),
            ("GapPatternA", GapPatternA(symbol=sym, long_only=True)),
        ]
        for label, strat in combos:
            r = _run(sym, strat, bars, EQUITY_SLIPPAGE_BPS)
            equity_all.setdefault(label, []).append(r)
            print(_row(label, r))

        print(SEP)

        # production combo: MA + Smash + Gap
        prod_strat = CompositeStrategy(sym, [
            MACrossover(symbol=sym),
            SmashDayB(symbol=sym, long_only=True),
            GapPatternA(symbol=sym, long_only=True),
        ])
        r = _run(sym, prod_strat, bars, EQUITY_SLIPPAGE_BPS)
        equity_all.setdefault("PROD: MA+Smash+Gap", []).append(r)
        print(_row("PROD: MA+Smash+Gap", r))

        # candidate: MA + Smash without Gap
        cand_strat = CompositeStrategy(sym, [
            MACrossover(symbol=sym),
            SmashDayB(symbol=sym, long_only=True),
        ])
        r = _run(sym, cand_strat, bars, EQUITY_SLIPPAGE_BPS)
        equity_all.setdefault("CAND: MA+Smash (no Gap)", []).append(r)
        print(_row("CAND: MA+Smash (no Gap)", r))

        print(_bh_row(bh_metrics))

    _summarise(equity_all)

    # ===== CRYPTO =====

    crypto_all: dict[str, list] = {}

    for sym in crypto_syms:
        bars = _fetch_crypto(sym, start, end, config)
        if bars.empty:
            print(f"  SKIP {sym}: no data")
            continue

        from trader.backtest.metrics import compute_metrics
        from trader.backtest.costs import CostModel
        from trader.backtest.engine import run_backtest
        bh_result = run_backtest(bars, CryptoEMACrossover(symbol=sym), cost_model=CostModel(slippage_bps=CRYPTO_SLIPPAGE_BPS))
        bh_metrics = compute_metrics(bh_result.buy_hold_curve, [])

        _section(f"CRYPTO — {sym}  (B&H: {bh_metrics.total_return:.1%})")

        # individual
        combos = [
            ("CryptoEMACrossover", CryptoEMACrossover(symbol=sym)),
            ("SmashDayB", SmashDayB(symbol=sym, long_only=True)),
            ("GapPatternA", GapPatternA(symbol=sym, long_only=True)),
        ]
        for label, strat in combos:
            r = _run(sym, strat, bars, CRYPTO_SLIPPAGE_BPS)
            crypto_all.setdefault(label, []).append(r)
            print(_row(label, r))

        print(SEP)

        # production combo: EMA + SmashDayB
        prod_strat = CompositeStrategy(sym, [
            CryptoEMACrossover(symbol=sym),
            SmashDayB(symbol=sym, long_only=True),
        ])
        r = _run(sym, prod_strat, bars, CRYPTO_SLIPPAGE_BPS)
        crypto_all.setdefault("PROD: EMA+Smash", []).append(r)
        print(_row("PROD: EMA+Smash", r))

        print(_bh_row(bh_metrics))

    _summarise(crypto_all)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
