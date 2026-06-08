"""EMA crossover strategy tuned for crypto assets.

Uses exponential moving averages (faster response to volatility than SMA) with
default periods 12/26 — the same windows used in MACD, well-established for crypto.

Logic mirrors MACrossover but uses ema() from indicators instead of sma().
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import ema


class CryptoEMACrossover(Strategy):
    def __init__(self, symbol: str, fast: int = 12, slow: int = 26) -> None:
        super().__init__(symbol)
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        # Handle empty data
        if bars.empty:
            return Signal(self.symbol, "hold", 0.0, "no bar data")

        close = bars["close"]
        if len(close) < self.slow:
            return Signal(self.symbol, "hold", 0.0, "insufficient history for EMA")

        fast_val = ema(close, self.fast).iloc[-1]
        slow_val = ema(close, self.slow).iloc[-1]
        if pd.isna(fast_val) or pd.isna(slow_val):
            return Signal(self.symbol, "hold", 0.0, "EMA not yet defined")

        spread = 0.0 if slow_val == 0 else (fast_val - slow_val) / slow_val
        strength = float(min(abs(spread) * 10.0, 1.0))

        if fast_val > slow_val:
            return Signal(
                self.symbol, "buy", strength,
                f"EMA{self.fast} {fast_val:.2f} > EMA{self.slow} {slow_val:.2f}",
            )
        if fast_val < slow_val:
            return Signal(
                self.symbol, "sell", strength,
                f"EMA{self.fast} {fast_val:.2f} < EMA{self.slow} {slow_val:.2f}",
            )
        return Signal(self.symbol, "hold", 0.0, "EMAs equal")
