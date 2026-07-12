"""Backtest candidate strategies on crypto against the production crypto stack.

Crypto drawdowns run far deeper than equities, so DipRecovery is swept over a
grid of dip/expansion parameters rather than tested only at the 10%/5% equity
defaults. The equity-stack strategies (SuperTrend, DonchianBreakout,
EquityBollingerReversion, HAPullback) are price-only and consume the same OHLCV
frame, so they run on crypto bars unchanged.

Sections per symbol:
  1. Production pair — CryptoEMACrossover, SmashDayB
  2. Candidates — equity strategies + DipRecovery parameter grid
  3. Combos — production stack + one candidate each

Usage:
    python scripts/backtest_crypto_candidates.py
    python scripts/backtest_crypto_candidates.py --years 4
    python scripts/backtest_crypto_candidates.py --crypto-symbols BTC/USD,ETH/USD
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DEFAULT_CRYPTO_SYMBOLS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD", "XRP/USD", "DOGE/USD", "AVAX/USD",
]
DEFAULT_YEARS = 2
CRYPTO_SLIPPAGE_BPS = 10.0
CRYPTO_TAKER_FEE_BPS = 25.0  # Alpaca crypto taker fee — real cost, not fee-free

DIP_GRID = [
    (0.10, 0.05),  # equity defaults, for reference
    (0.20, 0.05),
    (0.20, 0.10),
    (0.30, 0.10),
    (0.40, 0.10),
]


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
    result = run_backtest(bars, strategy, cost_model=CostModel(
        slippage_bps=CRYPTO_SLIPPAGE_BPS, taker_fee_bps=CRYPTO_TAKER_FEE_BPS))
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
    valid_bh = [r["bh"].total_return for rows in all_results.values() for r in rows if r]
    if valid_bh:
        print(f"  {'  avg buy & hold':<38}  {sum(valid_bh) / len(valid_bh):>8.1%}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Crypto candidate strategy backtest")
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
    symbols = (
        [s.strip() for s in args.crypto_symbols.split(",")]
        if args.crypto_symbols else DEFAULT_CRYPTO_SYMBOLS
    )

    from trader.config import load_config
    config = load_config()

    from trader.data.crypto_bars import get_crypto_bars
    from trader.strategy.crypto_trend import CryptoEMACrossover
    from trader.strategy.dip_recovery import DipRecovery
    from trader.strategy.donchian_breakout import DonchianBreakout
    from trader.strategy.equity_reversion import EquityBollingerReversion
    from trader.strategy.ha_pullback import HAPullback
    from trader.strategy.smash_day import SmashDayB
    from trader.strategy.supertrend import SuperTrend

    def prod(sym):
        return [
            CryptoEMACrossover(symbol=sym),
            SmashDayB(symbol=sym, long_only=True),
        ]

    print(f"\nCrypto Candidate Backtest  |  {start.date()} → {end.date()}")
    print(f"Symbols: {', '.join(symbols)}  |  Slippage: {CRYPTO_SLIPPAGE_BPS}bps")

    all_results: dict[str, list] = {}

    for sym in symbols:
        try:
            bars = get_crypto_bars(sym, start=start, end=end, config=config)
        except Exception as exc:
            print(f"  SKIP {sym}: {exc}")
            continue
        if bars.empty:
            print(f"  SKIP {sym}: no data")
            continue

        from trader.backtest.costs import CostModel
        from trader.backtest.engine import run_backtest
        from trader.backtest.metrics import compute_metrics
        bh_result = run_backtest(
            bars, CryptoEMACrossover(symbol=sym),
            cost_model=CostModel(slippage_bps=CRYPTO_SLIPPAGE_BPS,
                                 taker_fee_bps=CRYPTO_TAKER_FEE_BPS),
        )
        bh_metrics = compute_metrics(bh_result.buy_hold_curve, [])

        _section(f"CRYPTO — {sym}  ({len(bars)} bars, B&H: {bh_metrics.total_return:.1%})")

        combos = [
            ("CryptoEMACrossover (prod)", CryptoEMACrossover(symbol=sym)),
            ("SmashDayB (prod)", SmashDayB(symbol=sym, long_only=True)),
            ("SuperTrend", SuperTrend(symbol=sym)),
            ("DonchianBreakout", DonchianBreakout(symbol=sym)),
            ("EquityBollingerReversion", EquityBollingerReversion(symbol=sym)),
            ("HAPullback", HAPullback(symbol=sym)),
        ]
        combos += [
            (f"DipRecovery {int(d*100)}/{int(e*100)}",
             DipRecovery(symbol=sym, dip_pct=d, expansion_pct=e))
            for d, e in DIP_GRID
        ]
        for label, strat in combos:
            r = _run(strat, bars)
            all_results.setdefault(label, []).append(r)
            print(_row(label, r))

        print(SEP)

        stacks = [
            ("PROD: EMA+Smash", prod(sym)),
            ("PROD + Dip 20/10", prod(sym) + [DipRecovery(symbol=sym, dip_pct=0.20, expansion_pct=0.10)]),
            ("PROD + Dip 30/10", prod(sym) + [DipRecovery(symbol=sym, dip_pct=0.30, expansion_pct=0.10)]),
            ("PROD + SuperTrend", prod(sym) + [SuperTrend(symbol=sym)]),
            ("PROD + DonchianBreakout", prod(sym) + [DonchianBreakout(symbol=sym)]),
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
