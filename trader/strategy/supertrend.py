"""SuperTrend strategy — ATR-adaptive trend following with ADX regime filter.

Generates a buy signal when close is above the SuperTrend support line AND the
ADX confirms a trending market (ADX > threshold, default 20). Generates a sell
when close crosses below the SuperTrend line regardless of ADX (exits are not
filtered — always honor trend reversals).

This replaces MACrossover in the equity stack. SuperTrend adapts its band width
to recent volatility (via ATR), so it stays in valid trends longer and exits
faster on reversals than a fixed-window SMA crossover.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import adx, supertrend


class SuperTrend(Strategy):
    """SuperTrend trend-following strategy with ADX regime filter.

    Parameters
    ----------
    symbol:         ticker symbol
    atr_n:          ATR window for SuperTrend bands (default 14)
    multiplier:     ATR band multiplier (default 3.0)
    adx_threshold:  minimum ADX to permit a buy signal (default 20.0)
    """

    def __init__(
        self,
        symbol: str,
        atr_n: int = 14,
        multiplier: float = 3.0,
        adx_threshold: float = 20.0,
    ) -> None:
        super().__init__(symbol)
        self.atr_n = atr_n
        self.multiplier = multiplier
        self.adx_threshold = adx_threshold

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        if bars.empty:
            return Signal(self.symbol, "hold", 0.0, "no bar data")

        min_bars = self.atr_n * 2 + 1
        if len(bars) < min_bars:
            return Signal(self.symbol, "hold", 0.0, "insufficient history for SuperTrend")

        high = bars["high"]
        low = bars["low"]
        close = bars["close"]

        st_line, direction = supertrend(high, low, close, self.atr_n, self.multiplier)
        adx_val = adx(high, low, close, self.atr_n)

        curr_st = float(st_line.iloc[-1])
        curr_dir = float(direction.iloc[-1])
        curr_adx = float(adx_val.iloc[-1])
        curr_close = float(close.iloc[-1])

        if pd.isna(curr_st) or pd.isna(curr_dir) or pd.isna(curr_adx):
            return Signal(self.symbol, "hold", 0.0, "SuperTrend/ADX not yet defined")

        if curr_dir == 1.0:
            # Uptrend: close is above the SuperTrend support line.
            if curr_adx < self.adx_threshold:
                return Signal(
                    self.symbol, "hold", 0.0,
                    f"uptrend but ADX {curr_adx:.1f} < {self.adx_threshold} — choppy",
                )
            spread = (curr_close - curr_st) / curr_st if curr_st != 0.0 else 0.0
            strength = float(min(abs(spread) * 10.0, 1.0))
            return Signal(
                self.symbol, "buy", strength,
                f"ST {curr_st:.2f} < close {curr_close:.2f}, ADX {curr_adx:.1f}",
            )

        # Downtrend: close is below the SuperTrend resistance line.
        spread = (curr_st - curr_close) / curr_st if curr_st != 0.0 else 0.0
        strength = float(min(abs(spread) * 10.0, 1.0))
        return Signal(
            self.symbol, "sell", strength,
            f"ST {curr_st:.2f} > close {curr_close:.2f}, ADX {curr_adx:.1f}",
        )
