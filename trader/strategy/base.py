"""The Strategy contract.

A Strategy turns the bars available *as of* a point in time into a Signal. The same
interface is consumed by the backtest engine and (later) the live pipeline, which is
what guarantees sim and live cannot diverge.

THE NO-LOOKAHEAD RULE: `generate` may only use bars with index <= `asof`. The base
class enforces this by truncating before handing data to subclasses, so a strategy
*cannot* peek at the future even by accident.
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


class Strategy(ABC):
    """Base class for all strategies.

    Subclasses implement `_decide`, which receives ONLY the bars up to and including
    `asof`. Public `generate` does the truncation, so the no-lookahead guarantee lives
    in one place rather than relying on every subclass to behave.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def generate(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        asof = pd.Timestamp(asof)
        visible = bars.loc[bars.index <= asof]
        if visible.empty:
            return Signal(self.symbol, "hold", 0.0, "no data as of asof")
        return self._decide(visible, asof)

    @abstractmethod
    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        """Return a Signal using only `bars` (already truncated to index <= asof)."""
        raise NotImplementedError
