"""Grid-search a strategy's tunable parameters against real bars.

Generic CLI over trader/backtest/sweep.py — pass a strategy name (from
STRATEGY_FACTORIES below) and grid values per param, get back every combo's
metrics ranked by a chosen field.

Usage:
    python scripts/param_sweep.py --strategy dip_recovery --symbol AAPL \
        --grid dip_pct=0.05,0.08,0.10,0.15 --grid expansion_pct=0.03,0.05,0.08
    python scripts/param_sweep.py --strategy dip_recovery --symbol NVDA \
        --grid dip_pct=0.08,0.10,0.12 --metric calmar --top 5
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DEFAULT_YEARS = 2

# Maps a --strategy name to (constructor, {param: caster}) so grid values
# parsed from the CLI (always strings) land on the right type.
STRATEGY_FACTORIES = {
    "dip_recovery": ("trader.strategy.dip_recovery", "DipRecovery", {
        "dip_pct": float, "expansion_pct": float, "smooth_window": float,
    }),
    "supertrend": ("trader.strategy.supertrend", "SuperTrend", {
        "atr_n": int, "multiplier": float, "adx_threshold": float,
    }),
    "donchian_breakout": ("trader.strategy.donchian_breakout", "DonchianBreakout", {
        "channel_n": int, "trend_n": int, "time_exit": int,
    }),
}


def _parse_grid_arg(arg: str, casters: dict) -> tuple[str, list]:
    name, raw_values = arg.split("=", 1)
    name = name.strip()
    caster = casters.get(name)
    if caster is None:
        raise SystemExit(
            f"unknown param {name!r} for this strategy; known: {sorted(casters)}"
        )
    return name, [caster(v.strip()) for v in raw_values.split(",")]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", required=True, choices=sorted(STRATEGY_FACTORIES))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--grid", action="append", required=True, metavar="param=v1,v2,...",
                         help="repeatable; one strategy param per --grid flag")
    parser.add_argument("--metric", default="sharpe",
                         help="Metrics field to rank by (default: sharpe)")
    parser.add_argument("--top", type=int, default=10, help="rows to print (default: 10)")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args()

    import importlib
    module_name, class_name, casters = STRATEGY_FACTORIES[args.strategy]
    strategy_cls = getattr(importlib.import_module(module_name), class_name)

    param_grid: dict[str, list] = {}
    for grid_arg in args.grid:
        name, values = _parse_grid_arg(grid_arg, casters)
        param_grid[name] = values

    end = datetime.now()
    start = end - timedelta(days=365 * args.years)

    from trader.backtest.costs import CostModel
    from trader.backtest.sweep import param_sweep
    from trader.config import load_config
    from trader.data.alpaca_bars import get_daily_bars

    config = load_config()
    bars = get_daily_bars(args.symbol, start=start, end=end, config=config)
    if bars.empty:
        raise SystemExit(f"no data for {args.symbol}")

    def factory(**params):
        return strategy_cls(symbol=args.symbol, **params)

    results = param_sweep(
        bars, factory, param_grid,
        metric=args.metric,
        cost_model=CostModel(slippage_bps=args.slippage_bps),
    )

    print(f"\n{args.strategy} sweep on {args.symbol}  |  {start.date()} -> {end.date()}")
    print(f"Grid: {param_grid}  |  ranked by {args.metric}\n")
    for rank, r in enumerate(results[: args.top], start=1):
        params_str = ", ".join(f"{k}={v}" for k, v in r.params.items())
        print(
            f"  {rank:>2}. {params_str:<48}"
            f"  {args.metric}={getattr(r.metrics, args.metric):>8.3f}"
            f"  return={r.metrics.total_return:>7.1%}"
            f"  sharpe={r.metrics.sharpe:>6.2f}"
            f"  trades={r.metrics.turnover:>4d}"
        )


if __name__ == "__main__":
    main()
