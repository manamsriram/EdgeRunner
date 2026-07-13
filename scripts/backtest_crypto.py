"""Crypto strategy backtest: 2-year OOS comparison across 3 strategies and 6 symbols.

Compares CryptoEMACrossover, CryptoBollingerReversion, and CryptoReversalConfirmation
side-by-side against buy-and-hold for each symbol.

Usage:
    python scripts/backtest_crypto.py
    python scripts/backtest_crypto.py --symbols SOL/USD,LINK/USD --years 1
    python scripts/backtest_crypto.py --start 2024-01-01 --end 2025-12-31
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DEFAULT_SYMBOLS = ["SOL/USD", "LINK/USD", "XRP/USD", "BAT/USD", "FIL/USD", "SHIB/USD"]
DEFAULT_YEARS = 2


def _run_combo(symbol: str, strategy_factory, start: datetime, end: datetime, config):
    from trader.backtest.costs import CostModel
    from trader.backtest.engine import run_backtest
    from trader.backtest.metrics import compute_metrics
    from trader.data.crypto_bars import get_crypto_bars

    bars = get_crypto_bars(symbol, start=start, end=end, config=config)
    if len(bars) < 60:
        return None, f"{symbol}: only {len(bars)} bars (need 60)"

    strategy = strategy_factory(symbol)
    # Higher slippage for crypto + Alpaca crypto taker fee (~25 bps) so the numbers
    # reflect real trading costs, not a fee-free idealization.
    cost_model = CostModel(slippage_bps=10.0, taker_fee_bps=25.0)
    result = run_backtest(bars, strategy, cost_model=cost_model)

    strat = compute_metrics(result.equity_curve, result.trades)
    bh = compute_metrics(result.buy_hold_curve, [])
    return {"strat": strat, "bh": bh, "trades": len(result.trades), "bars": len(bars)}, None


def _row(label: str, strat, bh, trades: int) -> str:
    return (
        f"  {label:<32}"
        f"  {strat.total_return:>8.1%}"
        f"  {strat.sharpe:>7.2f}"
        f"  {strat.max_drawdown:>9.1%}"
        f"  {strat.win_rate:>8.1%}"
        f"  {trades:>7d}"
        f"  {'vs B&H ' + f'{strat.total_return - bh.total_return:+.1%}':>12}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Crypto strategy backtest")
    parser.add_argument("--symbols", default=None, help="Comma-separated crypto pairs")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    args = parser.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.utcnow()
    start = (
        datetime.strptime(args.start, "%Y-%m-%d")
        if args.start
        else end - timedelta(days=365 * args.years)
    )
    symbols = (
        [s.strip() for s in args.symbols.split(",")]
        if args.symbols
        else DEFAULT_SYMBOLS
    )

    try:
        from trader.config import load_config
        config = load_config()
    except Exception as exc:
        print(f"ERROR loading config: {exc}")
        return 2

    from trader.strategy.crypto_trend import CryptoEMACrossover
    from trader.strategy.smash_day import SmashDayB

    strategies = [
        ("EMACrossover", lambda sym: CryptoEMACrossover(symbol=sym)),
        ("SmashDayB", lambda sym: SmashDayB(symbol=sym, long_only=True)),
    ]

    header = (
        f"  {'strategy':<32}"
        f"  {'return':>8}"
        f"  {'sharpe':>7}"
        f"  {'max_dd':>9}"
        f"  {'win_rate':>8}"
        f"  {'trades':>7}"
        f"  {'vs B&H':>12}"
    )

    print(f"\nCrypto Backtest  |  {start.date()} → {end.date()}  |  slippage=10bps + taker=25bps")
    print(f"Symbols: {', '.join(symbols)}")

    all_results: dict[str, list] = {name: [] for name, _ in strategies}

    for symbol in symbols:
        print(f"\n{'='*80}")
        print(f"  {symbol}")
        print(header)
        print(f"  {'-'*110}")

        for name, factory in strategies:
            result, err = _run_combo(symbol, factory, start, end, config)
            if err:
                print(f"  {name:<32}  SKIP: {err}")
                continue
            print(_row(name, result["strat"], result["bh"], result["trades"]))
            all_results[name].append(result)

        # Buy & hold row for reference
        if all_results.get(strategies[0][0]):
            bh = all_results[strategies[0][0]][-1]["bh"]
            print(
                f"  {'buy & hold':<32}"
                f"  {bh.total_return:>8.1%}"
                f"  {bh.sharpe:>7.2f}"
                f"  {bh.max_drawdown:>9.1%}"
                f"  {'n/a':>8}"
                f"  {'0':>7}"
                f"  {'baseline':>12}"
            )

    # Summary: average metrics per strategy
    print(f"\n{'='*80}")
    print("  SUMMARY (averages across all symbols)")
    print(header)
    print(f"  {'-'*110}")
    for name, _ in strategies:
        results = all_results[name]
        if not results:
            print(f"  {name:<32}  no data")
            continue
        avg_return = sum(r["strat"].total_return for r in results) / len(results)
        avg_sharpe = sum(r["strat"].sharpe for r in results) / len(results)
        avg_dd = sum(r["strat"].max_drawdown for r in results) / len(results)
        avg_wr = sum(r["strat"].win_rate for r in results) / len(results)
        total_trades = sum(r["trades"] for r in results)
        avg_bh = sum(r["bh"].total_return for r in results) / len(results)
        vs_bh = avg_return - avg_bh

        print(
            f"  {name:<32}"
            f"  {avg_return:>8.1%}"
            f"  {avg_sharpe:>7.2f}"
            f"  {avg_dd:>9.1%}"
            f"  {avg_wr:>8.1%}"
            f"  {total_trades:>7d}"
            f"  {'vs B&H ' + f'{vs_bh:+.1%}':>12}"
        )

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
