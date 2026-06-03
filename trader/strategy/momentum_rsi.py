"""Momentum / RSI strategy.

Go long when medium-term momentum is positive and RSI is not overbought; exit (sell)
when momentum turns negative or RSI is overbought. Mean-reversion guardrails on top of
a momentum core. Price-only, fully backtestable.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import momentum, rsi


class MomentumRSI(Strategy):
    def __init__(
        self,
        symbol: str,
        lookback: int = 20,
        rsi_window: int = 14,
        rsi_overbought: float = 70.0,
        rsi_oversold: float = 30.0,
    ) -> None:
        super().__init__(symbol)
        self.lookback = lookback
        self.rsi_window = rsi_window
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        close = bars["close"]
        if len(close) <= max(self.lookback, self.rsi_window):
            return Signal(self.symbol, "hold", 0.0, "insufficient history")

        mom = momentum(close, self.lookback).iloc[-1]
        rsi_val = rsi(close, self.rsi_window).iloc[-1]
        if pd.isna(mom) or pd.isna(rsi_val):
            return Signal(self.symbol, "hold", 0.0, "indicators not yet defined")

        strength = float(min(abs(mom) * 5.0, 1.0))
        if mom > 0 and rsi_val < self.rsi_overbought:
            return Signal(self.symbol, "buy", strength,
                          f"mom {mom:+.2%}, RSI {rsi_val:.0f} (not overbought)")
        if mom < 0 or rsi_val >= self.rsi_overbought:
            return Signal(self.symbol, "sell", strength,
                          f"mom {mom:+.2%}, RSI {rsi_val:.0f}")
        return Signal(self.symbol, "hold", 0.0, f"mom {mom:+.2%}, RSI {rsi_val:.0f}")
