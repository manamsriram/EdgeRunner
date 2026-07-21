"""SMA crossover + Heikin Ashi pullback strategy.

Port of the "SMA Crossover + HA Pullback Strategy V2" PineScript, long side only
(the engine and live pipeline are long/flat). Entry requires, within a lookback
window after a fast/slow SMA bull cross:

  1. a pullback — real price tags the fast SMA after the cross,
  2. a Heikin Ashi bull candle whose HA close is back above the fast SMA,
  3. real close above the slow SMA (trend filter).

Exits mirror the Pine ATR stop / fixed-R target, evaluated on real closes, plus a
protective exit on a bear cross (the Pine original flips short there; long/flat
can only step aside). Heikin Ashi candles are derived from real OHLC, as in the
original's forced-standard-OHLC mode.

Stateful like DonchianBreakout: the entry close is recorded when the buy signal
fires so the stop/target levels are anchored; reset_state() clears it.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import atr, sma


def heikin_ashi_close_open(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Heikin Ashi close/open series computed from real OHLC."""
    ha_close = (bars["open"] + bars["high"] + bars["low"] + bars["close"]) / 4.0
    ha_open = pd.Series(index=bars.index, dtype=float)
    ha_open.iloc[0] = (bars["open"].iloc[0] + bars["close"].iloc[0]) / 2.0
    for i in range(1, len(bars)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2.0
    return ha_close, ha_open


class HAPullback(Strategy):
    """SMA bull-cross + pullback + Heikin Ashi confirmation, ATR stop / R target.

    Parameters
    ----------
    symbol:          ticker symbol
    fast:            fast SMA window (default 15)
    slow:            slow SMA window (default 50)
    cross_lookback:  bars after the cross during which an entry may trigger (default 20)
    atr_n:           ATR window for the stop (default 14)
    atr_mult:        ATR multiplier for the stop (default 1.5)
    rr:              reward:risk ratio for the target (default 2.0)
    """

    def __init__(
        self,
        symbol: str,
        fast: int = 15,
        slow: int = 50,
        cross_lookback: int = 20,
        atr_n: int = 14,
        atr_mult: float = 1.5,
        rr: float = 2.0,
    ) -> None:
        super().__init__(symbol)
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow
        self.cross_lookback = cross_lookback
        self.atr_n = atr_n
        self.atr_mult = atr_mult
        self.rr = rr
        self._entry_price: float | None = None
        self._stop: float | None = None
        self._target: float | None = None

    def reset_state(self) -> None:
        self._entry_price = None
        self._stop = None
        self._target = None

    def warm_up(self, bars: pd.DataFrame, *, has_position: bool = True) -> None:
        """Reconstruct ATR stop/target from the most recent historical entry signal.

        If the broker reports an open position, scan backward for the most recent
        bar that would have produced a buy signal, then restore the entry price,
        stop and target levels. If no entry can be reconstructed, state stays flat
        so the strategy can generate a fresh entry once conditions align.
        """
        self._warmed_up = True
        self.reset_state()
        if not has_position:
            return

        min_bars = max(self.slow, self.atr_n) + 2
        if len(bars) < min_bars:
            return

        # Pre-compute indicators once and slice backward to avoid O(N^2) work
        # inside the backward scan.
        full_sma_fast = sma(bars["close"], self.fast)
        full_sma_slow = sma(bars["close"], self.slow)
        full_ha_close, full_ha_open = heikin_ashi_close_open(bars)
        full_atr = atr(bars["high"], bars["low"], bars["close"], self.atr_n)

        # Scan backward from the bar before the current (live) one — a signal on
        # the current bar is still forming and should not be treated as an
        # established in-position entry on warm-up.
        if len(bars) - 1 < min_bars:
            return

        for i in range(len(bars) - 1, min_bars - 1, -1):
            sub_bars = bars.iloc[:i]
            _, levels = self._evaluate_entry(
                sub_bars,
                full_sma_fast.iloc[:i],
                full_sma_slow.iloc[:i],
                ha_close=full_ha_close.iloc[:i],
                ha_open=full_ha_open.iloc[:i],
                atr_val=full_atr.iloc[:i],
            )
            if levels is not None:
                self._entry_price, self._stop, self._target = levels
                return

    def _evaluate_entry(
        self,
        bars: pd.DataFrame,
        sma_fast: pd.Series,
        sma_slow: pd.Series,
        *,
        ha_close: pd.Series | None = None,
        ha_open: pd.Series | None = None,
        atr_val: pd.Series | None = None,
    ) -> tuple[Signal, tuple[float, float, float] | None]:
        """Return the buy signal and (entry, stop, target) if entry criteria are met.

        `ha_close`, `ha_open`, and `atr_val` may be precomputed for the bars to
        avoid redundant work during warm_up backward scans.
        """
        close = bars["close"]
        curr_close = float(close.iloc[-1])
        curr_fast = float(sma_fast.iloc[-1])
        curr_slow = float(sma_slow.iloc[-1])
        if pd.isna(curr_fast) or pd.isna(curr_slow):
            return Signal(self.symbol, "hold", 0.0, "SMAs not yet defined"), None

        # Bull cross within lookback.
        above = sma_fast > sma_slow
        crossed = above & ~above.shift(1, fill_value=False)
        cross_positions = crossed.values.nonzero()[0]
        if len(cross_positions) == 0:
            return Signal(self.symbol, "hold", 0.0, "no bull cross in history"), None
        last_cross = int(cross_positions[-1])
        bars_since_cross = len(bars) - 1 - last_cross
        if bars_since_cross > self.cross_lookback:
            return Signal(
                self.symbol, "hold", 0.0,
                f"bull cross {bars_since_cross} bars ago > lookback {self.cross_lookback}",
            ), None

        # Pullback: real low tags the fast SMA on some bar after the cross.
        post = bars.iloc[last_cross + 1:]
        post_fast = sma_fast.iloc[last_cross + 1:]
        pullback_done = bool((post["low"] <= post_fast).any()) if not post.empty else False
        if not pullback_done:
            return Signal(self.symbol, "hold", 0.0, "awaiting pullback to fast SMA"), None

        if ha_close is None or ha_open is None:
            ha_close, ha_open = heikin_ashi_close_open(bars.iloc[-self.slow:])
        curr_ha_close = float(ha_close.iloc[-1])
        curr_ha_open = float(ha_open.iloc[-1])
        ha_bull = curr_ha_close > curr_ha_open
        if not (ha_bull and curr_ha_close > curr_fast and curr_close > curr_slow):
            return Signal(self.symbol, "hold", 0.0, "awaiting HA bull confirmation above fast SMA"), None

        if atr_val is None:
            atr_val = atr(bars["high"], bars["low"], close, self.atr_n)
        curr_atr = float(atr_val.iloc[-1])
        if pd.isna(curr_atr) or curr_atr <= 0:
            return Signal(self.symbol, "hold", 0.0, "ATR not yet defined"), None

        entry = curr_close
        stop = curr_close - self.atr_mult * curr_atr
        target = curr_close + self.rr * self.atr_mult * curr_atr

        spread = (curr_close - curr_slow) / curr_slow if curr_slow != 0 else 0.0
        strength = float(min(max(abs(spread) * 10.0, 0.3), 1.0))
        sig = Signal(
            self.symbol, "buy", strength,
            f"HA pullback entry: cross {bars_since_cross} bars ago, "
            f"stop {stop:.2f}, target {target:.2f}",
        )
        return sig, (entry, stop, target)

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        min_bars = max(self.slow, self.atr_n) + 2
        if len(bars) < min_bars:
            return Signal(self.symbol, "hold", 0.0, "insufficient history for SMAs/ATR")

        close = bars["close"]
        sma_fast = sma(close, self.fast)
        sma_slow = sma(close, self.slow)
        curr_close = float(close.iloc[-1])
        curr_fast = float(sma_fast.iloc[-1])
        curr_slow = float(sma_slow.iloc[-1])
        if pd.isna(curr_fast) or pd.isna(curr_slow):
            return Signal(self.symbol, "hold", 0.0, "SMAs not yet defined")

        bear_cross_now = curr_fast < curr_slow

        # ---- Exit management while a long is open ----
        if self._entry_price is not None:
            if curr_close <= self._stop:  # type: ignore[operator]
                stop = self._stop
                self.reset_state()
                return Signal(self.symbol, "sell", 1.0,
                              f"ATR stop hit: close {curr_close:.2f} <= {stop:.2f}")
            if curr_close >= self._target:  # type: ignore[operator]
                target = self._target
                self.reset_state()
                return Signal(self.symbol, "sell", 1.0,
                              f"target hit: close {curr_close:.2f} >= {target:.2f}")
            if bear_cross_now:
                self.reset_state()
                return Signal(self.symbol, "sell", 0.8,
                              f"bear cross: SMA{self.fast} {curr_fast:.2f} < SMA{self.slow} {curr_slow:.2f}")
            return Signal(self.symbol, "hold", 0.0,
                          f"long open, stop {self._stop:.2f} / target {self._target:.2f}")

        if bear_cross_now:
            return Signal(self.symbol, "sell", 0.5,
                          f"SMA{self.fast} {curr_fast:.2f} < SMA{self.slow} {curr_slow:.2f}")

        # ---- Entry: bull cross within lookback + pullback + HA confirmation ----
        sig, levels = self._evaluate_entry(bars, sma_fast, sma_slow)
        if levels is not None:
            self._entry_price, self._stop, self._target = levels
        return sig
