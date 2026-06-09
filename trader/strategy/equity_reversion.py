"""Equity Bollinger Mean Reversion strategy.

Buys dips in a long-term uptrend using two oversold filters:
  1. Close below the lower Bollinger Band (statistically cheap)
  2. RSI(2) < 15 (short-term momentum exhausted)
  3. Close > SMA(200) (long-term uptrend confirmed — no catching falling knives)

Exits when RSI(2) > 85 (overbought) or close recovers to the mid Bollinger Band.
Conviction is proportional to the z-score magnitude of the dip.

This is anti-correlated with trend-following strategies (SmashDayB, SuperTrend):
it profits in the choppy mean-reverting conditions where trend followers lose.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import bollinger_bands, rsi, sma, zscore


class EquityBollingerReversion(Strategy):
    """Bollinger Band + RSI(2) mean reversion for equities.

    Parameters
    ----------
    symbol:         ticker symbol
    bb_window:      Bollinger Band SMA window (default 20)
    bb_std:         standard deviations for bands (default 2.0)
    rsi_window:     RSI window (default 2 — captures short-term exhaustion)
    rsi_entry:      RSI threshold to enter long (default 15)
    rsi_exit:       RSI threshold to exit long (default 85)
    trend_window:   SMA window for long-term trend filter (default 200)
    """

    def __init__(
        self,
        symbol: str,
        bb_window: int = 20,
        bb_std: float = 2.0,
        rsi_window: int = 2,
        rsi_entry: float = 15.0,
        rsi_exit: float = 85.0,
        trend_window: int = 200,
    ) -> None:
        super().__init__(symbol)
        self.bb_window = bb_window
        self.bb_std = bb_std
        self.rsi_window = rsi_window
        self.rsi_entry = rsi_entry
        self.rsi_exit = rsi_exit
        self.trend_window = trend_window
        self._in_position: bool = False

    def reset_state(self) -> None:
        self._in_position = False

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        if bars.empty:
            return Signal(self.symbol, "hold", 0.0, "no bar data")

        min_bars = self.trend_window + self.bb_window
        if len(bars) < min_bars:
            return Signal(self.symbol, "hold", 0.0, "insufficient history")

        close = bars["close"]
        upper_bb, mid_bb, lower_bb = bollinger_bands(close, self.bb_window, self.bb_std)
        rsi_val = rsi(close, self.rsi_window)
        sma_trend = sma(close, self.trend_window)
        zscore_val = zscore(close, self.bb_window)

        curr_close = float(close.iloc[-1])
        curr_rsi = float(rsi_val.iloc[-1])
        curr_lower = float(lower_bb.iloc[-1])
        curr_mid = float(mid_bb.iloc[-1])
        curr_sma = float(sma_trend.iloc[-1])
        curr_z = float(zscore_val.iloc[-1])

        if any(pd.isna(v) for v in [curr_rsi, curr_lower, curr_mid, curr_sma]):
            return Signal(self.symbol, "hold", 0.0, "indicators not yet defined")

        # EXIT: RSI overbought or price recovered to mid band.
        if self._in_position:
            if curr_rsi > self.rsi_exit:
                self._in_position = False
                return Signal(
                    self.symbol, "sell", 0.9,
                    f"reversion exit: RSI(2) {curr_rsi:.1f} > {self.rsi_exit}",
                )
            if curr_close > curr_mid:
                self._in_position = False
                return Signal(
                    self.symbol, "sell", 0.8,
                    f"reversion exit: close {curr_close:.2f} > mid-band {curr_mid:.2f}",
                )
            return Signal(self.symbol, "hold", 0.0, "reversion hold: waiting for exit")

        # ENTRY: oversold dip in long-term uptrend.
        below_lower = curr_close < curr_lower
        rsi_oversold = curr_rsi < self.rsi_entry
        in_uptrend = curr_close > curr_sma

        if below_lower and rsi_oversold and in_uptrend:
            strength = float(min(abs(curr_z) / 3.0, 1.0))
            self._in_position = True
            return Signal(
                self.symbol, "buy", strength,
                f"reversion buy: close {curr_close:.2f} < lower-BB {curr_lower:.2f}, "
                f"RSI(2) {curr_rsi:.1f}, z={curr_z:.2f}",
            )

        return Signal(self.symbol, "hold", 0.0, "no reversion setup")
