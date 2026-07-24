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

    def warm_up(self, bars: pd.DataFrame, *, has_position: bool = True) -> None:
        """Reconstruct entry state by scanning bar history after a cold start.

        Walks backward through all available bars looking for the most recent fresh
        Donchian breakout. If found, restores _entry_bar_ts and _entry_bar_low so
        time-exit and quick-exit logic resumes correctly rather than treating the
        position as a new entry opportunity.

        The scan is NOT bounded to `time_exit` bars — a restart can happen (and on
        this deployment, does happen, via frequent auto-deploys) long after the real
        entry bar. Bounding the scan to `time_exit` meant any entry older than that
        was silently un-trackable: _entry_bar_ts stayed None forever, exit logic
        never ran again for that position, and a later fresh breakout would just
        re-buy on top of it (observed in production: symbols bought 5x, sold 0x).
        If no breakout is found anywhere in the available history, the position
        predates the fetched window entirely — anchor to the oldest usable bar so
        the time-exit check fires on the very next tick instead of orphaning the
        position forever.
        """
        if not has_position:
            self._warmed_up = True
            return

        if self._entry_bar_ts is not None:
            self._warmed_up = True
            return

        min_bars = max(self.channel_n + 1, self.trend_n + 1) + 2
        if len(bars) < min_bars:
            self._warmed_up = True
            return

        close = bars["close"]
        low = bars["low"]
        scan_limit = len(bars) - min_bars

        # Only reconstruct entries that happened before the current (live) bar.
        # A breakout on the current bar is still forming and should not be
        # treated as an established in-position entry on warm-up.
        if scan_limit < 2:
            self._warmed_up = True
            return

        for lookback in range(2, scan_limit + 1):
            i = len(bars) - lookback
            subclose = close.iloc[: i + 1]
            sublow = low.iloc[: i + 1]

            if len(subclose) < min_bars:
                break

            curr_close = float(subclose.iloc[-1])
            prior_high = float(rolling_high(subclose.iloc[:-1], self.channel_n).iloc[-1])
            if pd.isna(prior_high):
                continue

            prev_close = float(subclose.iloc[-2])
            prior_prior_high = float(rolling_high(subclose.iloc[:-2], self.channel_n).iloc[-1])
            trend_ref = float(subclose.iloc[-(1 + self.trend_n)])

            fresh_breakout = pd.isna(prior_prior_high) or (prev_close <= prior_prior_high)
            if curr_close > prior_high and curr_close > trend_ref and fresh_breakout:
                self._entry_bar_ts = bars.index[i]
                self._entry_bar_low = float(sublow.iloc[-1])
                break
        else:
            # No breakout found anywhere in the fetched history — the position was
            # opened before the window we can see. Anchor to the oldest usable bar
            # so bars_held is already >= time_exit and _decide() force-exits on the
            # next tick rather than leaving the position untracked indefinitely.
            i = len(bars) - scan_limit
            self._entry_bar_ts = bars.index[i]
            self._entry_bar_low = float(low.iloc[i])

        self._warmed_up = True

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

        # Fresh-breakout check: only enter on the bar that *first* escapes the channel.
        # Prevents repeated buy signals on every bar of a continuous uptrend already above the high.
        # The prior bar must NOT already have been above the channel ceiling.
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
