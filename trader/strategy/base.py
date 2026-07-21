"""The Strategy contract.

A Strategy turns the bars available *as of* a point in time into a Signal. The same
interface is consumed by the backtest engine and (later) the live pipeline, which is
what guarantees sim and live cannot diverge.

THE NO-LOOKAHEAD RULE: `generate` / `generate_pair` may only use bars with index <=
`asof`. The base classes enforce this by truncating before handing data to subclasses,
so a strategy *cannot* peek at the future even by accident.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

Side = str  # "buy" | "sell" | "hold"


@dataclass(frozen=True)
class Signal:
    """A strategy's decision for one symbol at one point in time.

    strength is in [0, 1] and expresses conviction; it informs (future) position
    sizing but never bypasses the risk gate. reason is a short human-readable string
    for the run log and dashboard.
    """

    symbol: str
    side: Side
    strength: float
    reason: str

    def __post_init__(self) -> None:
        if self.side not in {"buy", "sell", "hold"}:
            raise ValueError(f"invalid side: {self.side!r}")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"strength must be in [0, 1], got {self.strength}")


@dataclass(frozen=True)
class PairSignal:
    """Decision for a two-legged pairs trade.

    Both legs must be acted on atomically — the pipeline checks both against the
    risk gate before submitting either, so a partial fill (one leg passes, one fails)
    is never allowed.
    """

    symbol_a: str
    side_a: Side
    symbol_b: str
    side_b: Side
    strength: float
    reason: str

    def __post_init__(self) -> None:
        for side in (self.side_a, self.side_b):
            if side not in {"buy", "sell", "hold"}:
                raise ValueError(f"invalid side: {side!r}")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"strength must be in [0, 1], got {self.strength}")

    @property
    def is_hold(self) -> bool:
        return self.side_a == "hold" or self.side_b == "hold"


class Strategy(ABC):
    """Base class for all single-symbol strategies.

    Subclasses implement `_decide`, which receives ONLY the bars up to and including
    `asof`. Public `generate` does the truncation, so the no-lookahead guarantee lives
    in one place rather than relying on every subclass to behave.
    """

    # Multiplier applied to the pipeline's stop-loss distance for positions this
    # strategy owns. 1.0 = the default stop (8% equity / 5% crypto). Strategies whose
    # thesis is buying into drawdown (DipRecovery) widen this so a normal dip doesn't
    # stop them out, while still keeping a catastrophe stop — a widened stop is not
    # no stop. Applies to BOTH the software stop and the broker-side GTC stop.
    stop_loss_multiplier: float = 1.0

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._warmed_up = False

    def warm_up(self, bars: pd.DataFrame, *, has_position: bool = True) -> None:
        """Reconstruct internal state from bar history after a cold start.

        Called by the pipeline on the first tick when the broker reports an open
        position for this symbol but the strategy instance is freshly created (e.g.
        after a process restart). Stateless strategies inherit this no-op; stateful
        strategies override it to restore their entry-tracking fields from bars.

        `has_position` is a defensive guard: implementations should only mark the
        strategy as "in a position" when the broker actually reports one. This avoids
        skipping valid entry opportunities if warm_up is ever called with no position.
        """
        self._warmed_up = True

    def generate(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        if bars.empty:
            return Signal(self.symbol, "hold", 0.0, "no bar data")
        asof = pd.Timestamp(asof)
        index_tz = getattr(bars.index, "tz", None)
        if index_tz is None and asof.tzinfo is not None:
            asof = asof.tz_localize(None)
        elif index_tz is not None and asof.tzinfo is None:
            asof = asof.tz_localize(index_tz)
        visible = bars.loc[bars.index <= asof]
        if visible.empty:
            return Signal(self.symbol, "hold", 0.0, f"no data as of {asof}")
        return self._decide(visible, asof)

    @abstractmethod
    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        """Return a Signal using only `bars` (already truncated to index <= asof)."""
        raise NotImplementedError


class IntradayStrategy(Strategy):
    """Base for intraday strategies. Pipeline routes via isinstance check."""
    pool: str = "intraday"
    eod_exit: bool = True
    skip_fundamental_gate: bool = True
    skip_overlay: bool = True
    bar_timeframe: str = "5min"
    lookback_minutes: int = 390


class PairStrategy(ABC):
    """Base class for two-symbol pairs strategies (stat arb, pairs trading).

    `generate_pair` enforces the same no-lookahead guarantee as `Strategy.generate`
    by truncating both bar series to `asof` before calling `_decide_pair`.

    `self.symbol` is set to "A:B" for logging / repo compatibility; it should not
    be used as a tradeable symbol.
    """

    def __init__(self, symbol_a: str, symbol_b: str) -> None:
        self.symbol_a = symbol_a
        self.symbol_b = symbol_b
        self.symbol = f"{symbol_a}:{symbol_b}"

    def generate_pair(
        self,
        bars_a: pd.DataFrame,
        bars_b: pd.DataFrame,
        asof: pd.Timestamp,
    ) -> PairSignal:
        asof = pd.Timestamp(asof)
        if bars_a.index.tz is None and asof.tzinfo is not None:
            asof = asof.tz_localize(None)
        elif bars_a.index.tz is not None and asof.tzinfo is None:
            asof = asof.tz_localize(bars_a.index.tz)

        visible_a = bars_a.loc[bars_a.index <= asof]
        visible_b = bars_b.loc[bars_b.index <= asof]
        if visible_a.empty or visible_b.empty:
            return PairSignal(
                self.symbol_a, "hold", self.symbol_b, "hold", 0.0,
                f"no data as of {asof}",
            )
        return self._decide_pair(visible_a, visible_b, asof)

    @abstractmethod
    def _decide_pair(
        self,
        bars_a: pd.DataFrame,
        bars_b: pd.DataFrame,
        asof: pd.Timestamp,
    ) -> PairSignal:
        """Return a PairSignal using only bars already truncated to index <= asof."""
        raise NotImplementedError
