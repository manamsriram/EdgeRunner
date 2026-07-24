"""SuperTrend strategy — ATR-adaptive trend following with ADX regime filter.

Generates a buy signal when close is above the SuperTrend support line AND the
ADX confirms a trending market (ADX > threshold, default 20) AND rising (trend
still building, not exhausted). Generates a sell when close crosses below the
SuperTrend line regardless of ADX (exits are not filtered — always honor trend
reversals).

This replaces MACrossover in the equity stack. SuperTrend adapts its band width
to recent volatility (via ATR), so it stays in valid trends longer and exits
faster on reversals than a fixed-window SMA crossover.

Production data (2026-07-23) showed nearly every closed SuperTrend trade exiting
via stop-loss, including symbols re-bought and re-stopped repeatedly (NTRBW 5x)
right after a loss — a plain `ADX > threshold` crossing doesn't distinguish a
trend just starting from one already stretched near its peak (where ADX tends
to be highest), and nothing stopped an immediate re-entry into the next
whipsaw. The ADX-rising check and re-entry cooldown below address that.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import adx, supertrend


class SuperTrend(Strategy):
    """SuperTrend trend-following strategy with ADX regime filter.

    Parameters
    ----------
    symbol:                 ticker symbol
    atr_n:                  ATR window for SuperTrend bands (default 14)
    multiplier:              ATR band multiplier (default 3.0)
    adx_threshold:           minimum ADX to permit a buy signal (default 20.0)
    adx_rising_lookback:     bars back ADX must have risen over to confirm the
                             trend is still building, not exhausted (default 3)
    reentry_cooldown_bars:   bars to hold off re-entry after any exit, to avoid
                             immediately re-buying into the same whipsaw (default 3)
    """

    def __init__(
        self,
        symbol: str,
        atr_n: int = 14,
        multiplier: float = 3.0,
        adx_threshold: float = 20.0,
        adx_rising_lookback: int = 3,
        reentry_cooldown_bars: int = 3,
    ) -> None:
        super().__init__(symbol)
        self.atr_n = atr_n
        self.multiplier = multiplier
        self.adx_threshold = adx_threshold
        self.adx_rising_lookback = adx_rising_lookback
        self.reentry_cooldown_bars = reentry_cooldown_bars
        self._cooldown_remaining = 0

    def reset_state(self) -> None:
        # Called by the pipeline on every confirmed exit (stop-loss, signal, EOD).
        # Block re-entry for a few bars so a stop-out can't immediately whipsaw
        # back into the same reversal.
        self._cooldown_remaining = self.reentry_cooldown_bars

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
            if self._cooldown_remaining > 0:
                self._cooldown_remaining -= 1
                return Signal(
                    self.symbol, "hold", 0.0,
                    f"uptrend but re-entry cooldown ({self._cooldown_remaining + 1} bars left)",
                )
            if curr_adx < self.adx_threshold:
                return Signal(
                    self.symbol, "hold", 0.0,
                    f"uptrend but ADX {curr_adx:.1f} < {self.adx_threshold} — choppy",
                )
            if len(adx_val) > self.adx_rising_lookback:
                prior_adx = float(adx_val.iloc[-(1 + self.adx_rising_lookback)])
                if not pd.isna(prior_adx) and curr_adx < prior_adx:
                    return Signal(
                        self.symbol, "hold", 0.0,
                        f"uptrend but ADX {curr_adx:.1f} falling vs "
                        f"{prior_adx:.1f} ({self.adx_rising_lookback} bars ago) — trend may be exhausted",
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
