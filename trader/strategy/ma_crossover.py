"""Moving-average crossover strategy.

Buy when the fast SMA is above the slow SMA, sell when it is below. A classic trend
follower — simple, fully backtestable on price alone, and a useful baseline.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import sma


class MACrossover(Strategy):
    def __init__(self, symbol: str, fast: int = 20, slow: int = 50) -> None:
        super().__init__(symbol)
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        close = bars["close"]
        if len(close) < self.slow:
            return Signal(self.symbol, "hold", 0.0, "insufficient history for SMA")

        fast_val = sma(close, self.fast).iloc[-1]
        slow_val = sma(close, self.slow).iloc[-1]
        if pd.isna(fast_val) or pd.isna(slow_val):
            return Signal(self.symbol, "hold", 0.0, "SMA not yet defined")

        # Conviction scales with the gap between the averages, capped at 1.
        spread = (fast_val - slow_val) / slow_val
        strength = float(min(abs(spread) * 10.0, 1.0))
        if fast_val > slow_val:
            return Signal(self.symbol, "buy", strength,
                          f"SMA{self.fast} {fast_val:.2f} > SMA{self.slow} {slow_val:.2f}")
        if fast_val < slow_val:
            return Signal(self.symbol, "sell", strength,
                          f"SMA{self.fast} {fast_val:.2f} < SMA{self.slow} {slow_val:.2f}")
        return Signal(self.symbol, "hold", 0.0, "SMAs equal")
