"""Smash Day Type B strategy (Oxford Capital Strategies, Rating: B).

A "smash" occurs when the prior bar's close breaks ABOVE the high from two bars ago
(long) or BELOW the low from two bars ago (short). This signals strong momentum: big
buyers stepped in and drove price through the entire prior range.

Type B is the strongest variant. Type A (close > prior close) rates C. Type C
(reversal) rates D. Only Type B with a trend filter reaches a B rating.

Lifecycle per position:
  1. Setup fires on bar i-1 → emit BUY on next tick
  2. Hold until:
     a. Quick exit: close drops below setup-bar's low (momentum failed)
     b. Time exit: after `time_exit` bars held

Reference: https://oxfordstrat.com/trading-strategies/smash-day-pattern-b1/
           https://oxfordstrat.com/trading-strategies/smash-day-pattern-b2/
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import atr


class SmashDayB(Strategy):
    """Smash Day Type B — long-only, daily bars.

    Parameters
    ----------
    symbol:     ticker symbol
    trend_n:    bars back for trend filter (close must be above close N bars ago)
    time_exit:  maximum bars to hold before forced exit
    atr_n:      ATR window for strength scaling
    long_only:  when True, short setups are ignored (default True for stocks/ETFs)
    """

    def __init__(
        self,
        symbol: str,
        trend_n: int = 20,
        time_exit: int = 10,
        atr_n: int = 14,
        long_only: bool = True,
    ) -> None:
        super().__init__(symbol)
        self.trend_n = trend_n
        self.time_exit = time_exit
        self.atr_n = atr_n
        self.long_only = long_only

        # State: set when a BUY signal is active.
        self._entry_bar_ts: pd.Timestamp | None = None
        self._entry_bar_low: float | None = None

    def reset_state(self) -> None:
        """Reset internal state variables to initial values."""
        self._entry_bar_ts = None
        self._entry_bar_low = None

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        # Handle empty data
        if bars.empty:
            return Signal(self.symbol, "hold", 0.0, "no bar data")

        min_bars = max(3, self.trend_n + 1, self.atr_n + 1)
        if len(bars) < min_bars:
            return Signal(self.symbol, "hold", 0.0, "insufficient history")

        close = bars["close"]
        high = bars["high"]
        low = bars["low"]

        # ------------------------------------------------------------------ #
        # EXIT LOGIC — runs first so a time/quick exit takes priority         #
        # ------------------------------------------------------------------ #
        if self._entry_bar_ts is not None:
            bars_after_entry = bars[bars.index > self._entry_bar_ts]
            bars_held = len(bars_after_entry)
            current_close = float(close.iloc[-1])

            # Quick exit: momentum failed — close dropped below setup bar's low.
            if current_close < self._entry_bar_low:  # type: ignore[operator]
                entry_low = self._entry_bar_low
                self._entry_bar_ts = None
                self._entry_bar_low = None
                return Signal(
                    self.symbol, "sell", 1.0,
                    f"smash-day-b quick exit: {current_close:.2f} < entry-low {entry_low:.2f}",
                )

            # Time exit: held long enough.
            if bars_held >= self.time_exit:
                self._entry_bar_ts = None
                self._entry_bar_low = None
                return Signal(
                    self.symbol, "sell", 0.8,
                    f"smash-day-b time exit: held {bars_held}/{self.time_exit} bars",
                )

            return Signal(
                self.symbol, "hold", 0.0,
                f"smash-day-b holding: bar {bars_held + 1}/{self.time_exit}",
            )

        # ------------------------------------------------------------------ #
        # ENTRY LOGIC — check setup on the last completed bar                 #
        # ------------------------------------------------------------------ #
        # bars[-1] = last completed bar (yesterday in live trading)
        # bars[-2] = two bars ago
        # bars[-3] = three bars ago — needed for High[i-2] reference
        if len(bars) < 3:
            return Signal(self.symbol, "hold", 0.0, "insufficient history")

        prev_close = float(close.iloc[-1])      # yesterday's close
        two_ago_high = float(high.iloc[-2])     # high from 2 bars ago
        two_ago_low = float(low.iloc[-2])       # low from 2 bars ago
        prev_low = float(low.iloc[-1])          # yesterday's low (for quick-exit ref)

        trend_ref = float(close.iloc[-(1 + self.trend_n)])
        atr_val = float(atr(high, low, close, self.atr_n).iloc[-1])

        # Long setup: yesterday's close broke above the high from 2 days ago.
        long_setup = prev_close > two_ago_high
        long_trend = prev_close > trend_ref

        if long_setup and long_trend:
            if pd.isna(atr_val) or atr_val == 0.0:
                return Signal(self.symbol, "hold", 0.0, "ATR not defined")
            # Strength = how far the smash extends above the prior high, in ATR units.
            smash_magnitude = (prev_close - two_ago_high) / atr_val
            strength = float(min(smash_magnitude, 1.0))
            self._entry_bar_ts = bars.index[-1]
            self._entry_bar_low = prev_low
            return Signal(
                self.symbol, "buy", strength,
                f"smash-day-b long: close {prev_close:.2f} > high[i-2] {two_ago_high:.2f} "
                f"(+{smash_magnitude:.2f} ATR), trend +{(prev_close / trend_ref - 1):.1%}",
            )

        # Short setup (futures/crypto only).
        if not self.long_only:
            short_setup = prev_close < two_ago_low
            short_trend = prev_close < trend_ref
            if short_setup and short_trend:
                if pd.isna(atr_val) or atr_val == 0.0:
                    return Signal(self.symbol, "hold", 0.0, "ATR not defined")
                smash_magnitude = (two_ago_low - prev_close) / atr_val
                strength = float(min(smash_magnitude, 1.0))
                self._entry_bar_ts = bars.index[-1]
                self._entry_bar_low = float(high.iloc[-1])  # quick exit above setup high
                return Signal(
                    self.symbol, "sell", strength,
                    f"smash-day-b short: close {prev_close:.2f} < low[i-2] {two_ago_low:.2f} "
                    f"({smash_magnitude:.2f} ATR), trend {(prev_close / trend_ref - 1):.1%}",
                )

        return Signal(self.symbol, "hold", 0.0, "no smash-day-b setup")
