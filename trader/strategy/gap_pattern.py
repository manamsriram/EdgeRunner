"""Gap Pattern Type A strategy (Oxford Capital Strategies, Rating: B).

A gap occurs when today's low is entirely above yesterday's high (gap up) or today's
high is entirely below yesterday's low (gap down). The trend filter — price above/below
the N-bar price channel — ensures we only trade gaps in the direction of momentum.

Oxford enters at the open of the gap bar itself. Our daily-bar framework generates
signals after bar close, so we detect the gap on bar i and enter at bar i+1's open
(one-day delay). The edge is preserved because gap continuation persists across days.

Exits:
  Pattern exit: gap filled — price closes below the pre-gap bar's high (long) or
                above the pre-gap bar's low (short). Momentum failed.
  Time exit:    close after `time_exit` bars regardless.

Reference: https://oxfordstrat.com/trading-strategies/gap-pattern/
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import rolling_high, rolling_low


class GapPatternA(Strategy):
    """Gap Pattern Type A — trend-filtered breakaway gap, daily bars.

    Parameters
    ----------
    symbol:     ticker symbol
    filter_n:   N-bar price channel window for trend filter
    time_exit:  maximum bars to hold before forced exit
    long_only:  when True, short setups are ignored (default True for stocks/ETFs)
    """

    def __init__(
        self,
        symbol: str,
        filter_n: int = 20,
        time_exit: int = 10,
        long_only: bool = True,
    ) -> None:
        super().__init__(symbol)
        self.filter_n = filter_n
        self.time_exit = time_exit
        self.long_only = long_only

        self._entry_bar_ts: pd.Timestamp | None = None
        # Price level where the gap started — used for pattern (gap-fill) exit.
        # Long: high of the bar before the gap. Short: low of the bar before the gap.
        self._gap_ref_level: float | None = None

    def reset_state(self) -> None:
        """Reset internal state variables to initial values."""
        self._entry_bar_ts = None
        self._gap_ref_level = None

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        # Handle empty data
        if bars.empty:
            return Signal(self.symbol, "hold", 0.0, "no bar data")

        min_bars = self.filter_n + 2  # need filter_n prior bars + pre-gap bar + gap bar
        if len(bars) < min_bars:
            return Signal(self.symbol, "hold", 0.0, "insufficient history")
</new_string>

        close = bars["close"]
        high = bars["high"]
        low = bars["low"]

        # ------------------------------------------------------------------ #
        # EXIT LOGIC                                                           #
        # ------------------------------------------------------------------ #
        if self._entry_bar_ts is not None:
            bars_after = bars[bars.index > self._entry_bar_ts]
            bars_held = len(bars_after)
            current_close = float(close.iloc[-1])
            gap_ref = self._gap_ref_level  # type: ignore[assignment]

            # Pattern exit: gap filled.
            if current_close < gap_ref:
                self._entry_bar_ts = None
                self._gap_ref_level = None
                return Signal(
                    self.symbol, "sell", 1.0,
                    f"gap-a pattern exit: {current_close:.2f} < gap-ref {gap_ref:.2f}",
                )

            if bars_held >= self.time_exit:
                self._entry_bar_ts = None
                self._gap_ref_level = None
                return Signal(
                    self.symbol, "sell", 0.8,
                    f"gap-a time exit: held {bars_held}/{self.time_exit} bars",
                )

            return Signal(
                self.symbol, "hold", 0.0,
                f"gap-a holding: bar {bars_held + 1}/{self.time_exit}",
            )

        # ------------------------------------------------------------------ #
        # ENTRY LOGIC                                                          #
        # ------------------------------------------------------------------ #
        # bars[-1] = last completed bar (the potential gap bar)
        # bars[-2] = bar immediately before the gap
        prev_low = float(low.iloc[-1])
        prev_high = float(high.iloc[-1])
        prev_close = float(close.iloc[-1])
        two_ago_high = float(high.iloc[-2])
        two_ago_low = float(low.iloc[-2])

        # Trend filter uses rolling channel on all bars BEFORE the gap bar so
        # the comparison is never trivially satisfied by the current bar itself.
        prior_high_channel = float(rolling_high(high.iloc[:-1], self.filter_n).iloc[-1])
        prior_low_channel = float(rolling_low(low.iloc[:-1], self.filter_n).iloc[-1])

        if pd.isna(prior_high_channel) or pd.isna(prior_low_channel):
            return Signal(self.symbol, "hold", 0.0, "channel not yet defined")

        # Long: gap up AND close above N-bar high channel (uptrend).
        long_gap = prev_low > two_ago_high
        long_trend = prev_close > prior_high_channel

        if long_gap and long_trend:
            gap_size_pct = (prev_low - two_ago_high) / two_ago_high
            strength = float(min(gap_size_pct * 20.0, 1.0))  # 5% gap → strength 1.0
            self._entry_bar_ts = bars.index[-1]
            self._gap_ref_level = two_ago_high
            return Signal(
                self.symbol, "buy", strength,
                f"gap-a long: low {prev_low:.2f} > prev-high {two_ago_high:.2f} "
                f"(+{gap_size_pct:.1%}), close {prev_close:.2f} > "
                f"{self.filter_n}-bar channel {prior_high_channel:.2f}",
            )

        if not self.long_only:
            # Short: gap down AND close below N-bar low channel (downtrend).
            short_gap = prev_high < two_ago_low
            short_trend = prev_close < prior_low_channel

            if short_gap and short_trend:
                gap_size_pct = (two_ago_low - prev_high) / two_ago_low
                strength = float(min(gap_size_pct * 20.0, 1.0))
                self._entry_bar_ts = bars.index[-1]
                self._gap_ref_level = two_ago_low
                return Signal(
                    self.symbol, "sell", strength,
                    f"gap-a short: high {prev_high:.2f} < prev-low {two_ago_low:.2f} "
                    f"(-{gap_size_pct:.1%}), close {prev_close:.2f} < "
                    f"{self.filter_n}-bar channel {prior_low_channel:.2f}",
                )

        return Signal(self.symbol, "hold", 0.0, "no gap-a setup")
