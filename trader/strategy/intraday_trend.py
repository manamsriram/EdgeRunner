"""IntradayTrend — SuperTrend on 5-min bars. Logic identical to SuperTrend daily strategy."""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import IntradayStrategy, Signal
from trader.strategy.indicators import adx, supertrend


class IntradayTrend(IntradayStrategy):
    """SuperTrend trend-following on 5-min intraday bars with ADX regime filter."""

    bar_timeframe = "5min"

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
        min_bars = self.atr_n * 2 + 1
        if len(bars) < min_bars:
            return Signal(self.symbol, "hold", 0.0, "insufficient history for IntradayTrend")

        high, low, close = bars["high"], bars["low"], bars["close"]
        st_line, direction = supertrend(high, low, close, self.atr_n, self.multiplier)
        adx_val = adx(high, low, close, self.atr_n)

        curr_st = float(st_line.iloc[-1])
        curr_dir = float(direction.iloc[-1])
        curr_adx = float(adx_val.iloc[-1])
        curr_close = float(close.iloc[-1])

        if pd.isna(curr_st) or pd.isna(curr_dir) or pd.isna(curr_adx):
            return Signal(self.symbol, "hold", 0.0, "SuperTrend/ADX not yet defined")

        if curr_dir == 1.0:
            if curr_adx < self.adx_threshold:
                return Signal(
                    self.symbol, "hold", 0.0,
                    f"uptrend but ADX {curr_adx:.1f} < {self.adx_threshold} — choppy",
                )
            spread = (curr_close - curr_st) / curr_st if curr_st != 0.0 else 0.0
            return Signal(
                self.symbol, "buy", float(min(abs(spread) * 10.0, 1.0)),
                f"ST {curr_st:.2f} < close {curr_close:.2f}, ADX {curr_adx:.1f}",
            )

        spread = (curr_st - curr_close) / curr_st if curr_st != 0.0 else 0.0
        return Signal(
            self.symbol, "sell", float(min(abs(spread) * 10.0, 1.0)),
            f"ST {curr_st:.2f} > close {curr_close:.2f}, ADX {curr_adx:.1f}",
        )
