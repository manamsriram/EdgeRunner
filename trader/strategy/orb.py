"""OpeningRangeBreakout — buy close above ORH with volume; sell below ORL or EOD."""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import IntradayStrategy, Signal

_RANGE_BARS = 30          # bars 0-29 = 9:30-9:59 AM
_VOLUME_MULTIPLIER = 1.5


class OpeningRangeBreakout(IntradayStrategy):
    """Enter long on first close above opening range high (ORH) with volume confirmation.

    Range forms during bars 0-29 (first 30 minutes). No re-entry after exit.
    """

    bar_timeframe = "1min"

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self._orh: float = 0.0
        self._orl: float = 0.0
        self._range_set: bool = False
        self._entered: bool = False
        self._exited: bool = False
        self._last_session_date = None

    def warm_up(self, bars: pd.DataFrame) -> None:
        self._entered = True
        self._warmed_up = True

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        _today = asof.date() if hasattr(asof, "date") else bars.index[-1].date()
        if self._last_session_date != _today:
            self._range_set = False
            self._orh = 0.0
            self._orl = 0.0
            self._entered = False
            self._exited = False
            self._last_session_date = _today

        curr_idx = len(bars) - 1
        curr_close = float(bars["close"].iloc[-1])

        if not self._range_set:
            if curr_idx < _RANGE_BARS - 1:
                return Signal(
                    self.symbol, "hold", 0.0,
                    f"forming opening range (bar {curr_idx}/{_RANGE_BARS - 1})",
                )
            range_bars = bars.iloc[:_RANGE_BARS]
            self._orh = float(range_bars["high"].max())
            self._orl = float(range_bars["low"].min())
            self._range_set = True

        if self._entered:
            if curr_close < self._orl:
                self._entered = False
                self._exited = True
                return Signal(
                    self.symbol, "sell", 1.0,
                    f"ORB violated: close {curr_close:.2f} < ORL {self._orl:.2f}",
                )
            return Signal(self.symbol, "hold", 0.0, "ORB trade active — holding")

        if self._exited:
            return Signal(self.symbol, "hold", 0.0, "no re-entry after ORB exit")

        if curr_close <= self._orh:
            return Signal(
                self.symbol, "hold", 0.0,
                f"no breakout: close {curr_close:.2f} <= ORH {self._orh:.2f}",
            )

        avg_volume = float(bars["volume"].mean())
        entry_volume = float(bars["volume"].iloc[-1])
        if entry_volume < avg_volume * _VOLUME_MULTIPLIER:
            return Signal(
                self.symbol, "hold", 0.0,
                f"breakout volume {entry_volume:,.0f} < {_VOLUME_MULTIPLIER}x avg",
            )

        self._entered = True
        strength = float(min((curr_close - self._orh) / self._orh, 1.0))
        return Signal(
            self.symbol, "buy", max(strength, 0.01),
            f"ORB breakout: close {curr_close:.2f} > ORH {self._orh:.2f}",
        )
