"""Donchian Channel Breakout strategy.

Enters long when the close breaks above the prior N-bar rolling high (Donchian
channel). Unlike GapPatternA, there is no structural one-day delay — the signal
fires on the bar that prints the breakout close.

Trend filter: close > close[-(1 + trend_n)] ensures we only chase breakouts
in the direction of the prevailing trend (same filter as SmashDayB).

Exits:
  Quick exit: close drops below the entry bar's low (momentum failed).
  Time exit:  forced exit after `time_exit` bars.

Reference: Donchian channel breakout (Richard Donchian, 1950s).
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import rolling_high


class DonchianBreakout(Strategy):
    """Donchian Channel Breakout — long-only, daily bars.

    Parameters
    ----------
    symbol:     ticker symbol
    channel_n:  lookback window for the Donchian channel (default 20)
    trend_n:    bars back for trend filter (default 20)
    time_exit:  maximum bars to hold before forced exit (default 10)
    """

    def __init__(
        self,
        symbol: str,
        channel_n: int = 20,
        trend_n: int = 20,
        time_exit: int = 10,
    ) -> None:
        super().__init__(symbol)
        self.channel_n = channel_n
        self.trend_n = trend_n
        self.time_exit = time_exit
        self._entry_bar_ts: pd.Timestamp | None = None
        self._entry_bar_low: float | None = None

    def reset_state(self) -> None:
        self._entry_bar_ts = None
        self._entry_bar_low = None

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        if bars.empty:
            return Signal(self.symbol, "hold", 0.0, "no bar data")

        min_bars = max(self.channel_n + 1, self.trend_n + 1)
        if len(bars) < min_bars:
            return Signal(self.symbol, "hold", 0.0, "insufficient history")

        close = bars["close"]
        low = bars["low"]

        # ------------------------------------------------------------------ #
        # EXIT LOGIC                                                           #
        # ------------------------------------------------------------------ #
        if self._entry_bar_ts is not None:
            bars_after = bars[bars.index > self._entry_bar_ts]
            bars_held = len(bars_after)
            curr_close = float(close.iloc[-1])

            if curr_close < self._entry_bar_low:  # type: ignore[operator]
                entry_low = self._entry_bar_low
                self._entry_bar_ts = None
                self._entry_bar_low = None
                return Signal(
                    self.symbol, "sell", 1.0,
                    f"donchian quick exit: {curr_close:.2f} < entry-low {entry_low:.2f}",
                )

            if bars_held >= self.time_exit:
                self._entry_bar_ts = None
                self._entry_bar_low = None
                return Signal(
                    self.symbol, "sell", 0.8,
                    f"donchian time exit: held {bars_held}/{self.time_exit} bars",
                )

            return Signal(
                self.symbol, "hold", 0.0,
                f"donchian holding: bar {bars_held + 1}/{self.time_exit}",
            )

        # ------------------------------------------------------------------ #
        # ENTRY LOGIC                                                          #
        # ------------------------------------------------------------------ #
        curr_close = float(close.iloc[-1])
        curr_low = float(low.iloc[-1])

        # Prior N-bar high excludes the current bar (no self-reference).
        prior_high = float(rolling_high(close.iloc[:-1], self.channel_n).iloc[-1])

        trend_ref = float(close.iloc[-(1 + self.trend_n)])

        if pd.isna(prior_high):
            return Signal(self.symbol, "hold", 0.0, "Donchian channel not yet defined")

        # Fresh-breakout check: the prior bar must NOT already have been above the
        # channel ceiling. This prevents firing on every bar of a continuous uptrend
        # — only the first bar that escapes the channel is a true Donchian breakout.
        prev_close = float(close.iloc[-2])
        prior_prior_high = float(rolling_high(close.iloc[:-2], self.channel_n).iloc[-1])
        fresh_breakout = pd.isna(prior_prior_high) or (prev_close <= prior_prior_high)

        breakout = curr_close > prior_high
        uptrend = curr_close > trend_ref

        if breakout and uptrend and fresh_breakout:
            breakout_pct = (curr_close - prior_high) / prior_high
            strength = float(min(breakout_pct * 20.0, 1.0))
            self._entry_bar_ts = bars.index[-1]
            self._entry_bar_low = curr_low
            return Signal(
                self.symbol, "buy", strength,
                f"donchian breakout: close {curr_close:.2f} > {self.channel_n}-bar high {prior_high:.2f} "
                f"(+{breakout_pct:.1%})",
            )

        return Signal(self.symbol, "hold", 0.0, "no donchian breakout")
