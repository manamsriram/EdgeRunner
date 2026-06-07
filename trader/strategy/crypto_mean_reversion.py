"""Bollinger Band mean-reversion strategy for crypto assets.

Buy when price crosses below the lower band (oversold); sell when price crosses
above the upper band (overbought). Conviction scales with how far price has moved
from the mid band relative to the band width.

Works best in ranging/sideways markets; combine with a trend filter (e.g.
CryptoEMACrossover) to avoid trading against strong trends.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import bollinger_bands


class CryptoBollingerReversion(Strategy):
    def __init__(
        self,
        symbol: str,
        window: int = 20,
        num_std: float = 2.0,
    ) -> None:
        super().__init__(symbol)
        if window < 2:
            raise ValueError("window must be >= 2")
        self.window = window
        self.num_std = num_std

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        close = bars["close"]
        if len(close) < self.window:
            return Signal(self.symbol, "hold", 0.0, "insufficient history for Bollinger Bands")

        upper, mid, lower = bollinger_bands(close, self.window, self.num_std)
        price = close.iloc[-1]
        upper_val = upper.iloc[-1]
        mid_val = mid.iloc[-1]
        lower_val = lower.iloc[-1]

        if pd.isna(upper_val) or pd.isna(lower_val):
            return Signal(self.symbol, "hold", 0.0, "Bollinger Bands not yet defined")

        band_width = upper_val - lower_val
        if band_width <= 0:
            return Signal(self.symbol, "hold", 0.0, "zero band width")

        strength = float(min(abs(price - mid_val) / band_width, 1.0))

        if price <= lower_val:
            return Signal(
                self.symbol, "buy", strength,
                f"price {price:.2f} <= lower band {lower_val:.2f} (oversold)",
            )
        if price >= upper_val:
            return Signal(
                self.symbol, "sell", strength,
                f"price {price:.2f} >= upper band {upper_val:.2f} (overbought)",
            )
        return Signal(
            self.symbol, "hold", 0.0,
            f"price {price:.2f} within bands [{lower_val:.2f}, {upper_val:.2f}]",
        )
