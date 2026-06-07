"""Strategy contract + concrete quant signals.

The `Strategy` interface in `base` is the single contract reused by the backtest now
and the live pipeline later, so simulated and live decisions cannot drift apart.
"""
from trader.strategy.base import Signal, Strategy
from trader.strategy.gap_pattern import GapPatternA
from trader.strategy.ma_crossover import MACrossover
from trader.strategy.momentum_rsi import MomentumRSI
from trader.strategy.smash_day import SmashDayB

__all__ = ["GapPatternA", "MACrossover", "MomentumRSI", "Signal", "SmashDayB", "Strategy"]
