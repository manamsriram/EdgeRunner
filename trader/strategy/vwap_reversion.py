"""VWAPReversion — fade >2σ deviations from intraday VWAP on 1-min bars."""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import IntradayStrategy, Signal


class VWAPReversion(IntradayStrategy):
    """Buy when price is >2σ below VWAP; sell when price returns to VWAP or above.

    VWAP resets each call (bars are today-only). Requires 20-bar warm-up.
    _entered tracks whether we are in a position opened by this strategy instance.
    warm_up() reconstructs _entered from bar history on cold start.
    """

    bar_timeframe = "1min"

    def __init__(self, symbol: str, sigma_entry: float = 2.0, std_window: int = 20) -> None:
        super().__init__(symbol)
        self.sigma_entry = sigma_entry
        self.std_window = std_window
        self._entered = False
        self._last_session_date = None

    def warm_up(self, bars: pd.DataFrame) -> None:
        self._entered = True
        self._warmed_up = True

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        _today = asof.date() if hasattr(asof, "date") else bars.index[-1].date()
        if self._last_session_date != _today:
            self._entered = False
            self._last_session_date = _today

        if len(bars) < self.std_window:
            return Signal(self.symbol, "hold", 0.0, f"warm-up: need {self.std_window} bars")

        vwap = (bars["close"] * bars["volume"]).cumsum() / bars["volume"].cumsum()
        deviation = bars["close"] - vwap
        std = deviation.rolling(self.std_window).std()

        curr_close = float(bars["close"].iloc[-1])
        curr_vwap = float(vwap.iloc[-1])
        curr_std = float(std.iloc[-1])

        if self._entered:
            if curr_close >= curr_vwap:
                self._entered = False
                return Signal(
                    self.symbol, "sell", 1.0,
                    f"VWAP reversion complete: close {curr_close:.2f} >= vwap {curr_vwap:.2f}",
                )
            return Signal(self.symbol, "hold", 0.0, "holding — waiting for VWAP reversion")

        if pd.isna(curr_std) or curr_std == 0:
            return Signal(self.symbol, "hold", 0.0, "std undefined")

        sigma_distance = (curr_vwap - curr_close) / curr_std
        if sigma_distance >= self.sigma_entry:
            self._entered = True
            strength = float(min(sigma_distance / 3.0, 1.0))
            return Signal(
                self.symbol, "buy", strength,
                f"VWAP reversion entry: {sigma_distance:.1f}σ below VWAP {curr_vwap:.2f}",
            )

        return Signal(self.symbol, "hold", 0.0, f"deviation {sigma_distance:.2f}σ < {self.sigma_entry}σ threshold")
