"""GapAndGo — pre-market gap momentum strategy on 1-min bars.

Entry: bar 5-9 only (9:35-9:39 AM ET). prev_close injected by pipeline from daily bars cache.
Exit: close < entry_bar_open (momentum faded) or EOD exit from pipeline.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import IntradayStrategy, Signal

_ENTRY_BAR_START = 5   # bar index 5 = 9:35 AM (0-indexed from 9:30)
_ENTRY_BAR_END = 9     # bar index 9 = 9:39 AM (inclusive)
_GAP_MIN_PCT = 0.02    # 2% gap minimum
_VOLUME_MULTIPLIER = 1.5


class GapAndGo(IntradayStrategy):
    """Enter long on gap-up days when gap holds and volume confirms at 9:35 AM."""

    bar_timeframe = "1min"

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self.prev_close: float = 0.0
        self._entered = False
        self._entry_bar_open: float = 0.0
        self._entry_attempted = False
        self._last_session_date = None

    def warm_up(self, bars: pd.DataFrame, *, has_position: bool = True) -> None:
        # Only mark as entered if the broker actually reports an open position;
        # otherwise a restarted process would skip valid entry signals.
        self._entered = has_position
        self._entry_bar_open = float(bars["close"].iloc[-1]) if has_position else 0.0
        self._warmed_up = True

    def reset_state(self) -> None:
        self._entered = False
        self._entry_bar_open = 0.0
        self._entry_attempted = False

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        _today = asof.date() if hasattr(asof, "date") else bars.index[-1].date()
        if self._last_session_date != _today:
            self._entry_attempted = False
            self._entered = False
            self._last_session_date = _today

        if self.prev_close <= 0.0:
            return Signal(self.symbol, "hold", 0.0, "prev_close not set")

        curr_idx = len(bars) - 1
        curr_close = float(bars["close"].iloc[-1])

        if self._entered:
            if curr_close < self._entry_bar_open:
                self._entered = False
                return Signal(
                    self.symbol, "sell", 1.0,
                    f"gap momentum faded: close {curr_close:.2f} < entry open {self._entry_bar_open:.2f}",
                )
            return Signal(self.symbol, "hold", 0.0, "gap trade active — holding")

        # Window closed or already attempted — no new entries.
        if self._entry_attempted or curr_idx > _ENTRY_BAR_END:
            return Signal(self.symbol, "hold", 0.0, "entry window closed")

        if curr_idx < _ENTRY_BAR_START:
            return Signal(self.symbol, "hold", 0.0, f"waiting for entry window (bar {curr_idx})")

        # Entry window: bars 5-9.
        first_open = float(bars["open"].iloc[0])
        gap_pct = (first_open - self.prev_close) / self.prev_close

        if gap_pct < _GAP_MIN_PCT:
            self._entry_attempted = True
            return Signal(
                self.symbol, "hold", 0.0,
                f"gap {gap_pct:.2%} < {_GAP_MIN_PCT:.0%} minimum",
            )

        avg_volume = float(bars["volume"].mean())
        entry_volume = float(bars["volume"].iloc[curr_idx])
        if entry_volume < avg_volume * _VOLUME_MULTIPLIER:
            return Signal(
                self.symbol, "hold", 0.0,
                f"entry volume {entry_volume:,.0f} < {_VOLUME_MULTIPLIER}x avg {avg_volume:,.0f}",
            )

        if curr_close <= self.prev_close:
            return Signal(
                self.symbol, "hold", 0.0,
                f"price {curr_close:.2f} not holding above prev_close {self.prev_close:.2f}",
            )

        self._entered = True
        self._entry_attempted = True
        self._entry_bar_open = float(bars["open"].iloc[curr_idx])
        return Signal(
            self.symbol, "buy", min(gap_pct / 0.05, 1.0),
            f"gap {gap_pct:.2%} confirmed at bar {curr_idx}, vol {entry_volume:,.0f}",
        )
