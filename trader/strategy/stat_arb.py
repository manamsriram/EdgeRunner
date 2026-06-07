"""Statistical arbitrage (pairs trading) strategy.

Trades the mean-reverting spread between two correlated assets (e.g. BTC/USD and
ETH/USD). The spread is defined as log(price_a / price_b). A rolling z-score
measures how far the spread has deviated from its historical mean.

Signals:
  z > +entry_z  → spread too wide (A expensive vs B) → sell A, buy B
  z < -entry_z  → spread too narrow (A cheap vs B)   → buy A, sell B
  |z| < exit_z  → spread has reverted → hold (let existing position ride)

This is statistical arbitrage, not pure arbitrage. It exploits a statistical
tendency for correlated asset prices to revert toward their historical ratio — it
is not risk-free and can lose money if the pair relationship breaks down.

Runs via run_pair_pipeline in pipeline.py, which enforces atomic execution: both
legs pass the risk gate or neither submits.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from trader.strategy.base import PairSignal, PairStrategy
from trader.strategy.indicators import zscore


class StatArbPair(PairStrategy):
    def __init__(
        self,
        symbol_a: str,
        symbol_b: str,
        window: int = 30,
        entry_z: float = 2.0,
        exit_z: float = 0.5,
    ) -> None:
        super().__init__(symbol_a, symbol_b)
        if window < 5:
            raise ValueError("window must be >= 5")
        if entry_z <= exit_z:
            raise ValueError("entry_z must be > exit_z")
        self.window = window
        self.entry_z = entry_z
        self.exit_z = exit_z

    def _decide_pair(
        self,
        bars_a: pd.DataFrame,
        bars_b: pd.DataFrame,
        asof: pd.Timestamp,
    ) -> PairSignal:
        close_a = bars_a["close"]
        close_b = bars_b["close"]

        if len(close_a) < self.window or len(close_b) < self.window:
            return PairSignal(
                self.symbol_a, "hold", self.symbol_b, "hold", 0.0,
                "insufficient history for stat arb",
            )

        # Align on common dates (bars may have different trading calendars).
        common = close_a.index.intersection(close_b.index)
        if len(common) < self.window:
            return PairSignal(
                self.symbol_a, "hold", self.symbol_b, "hold", 0.0,
                f"insufficient shared dates ({len(common)} < {self.window})",
            )
        a = close_a.loc[common]
        b = close_b.loc[common]

        spread = np.log(a / b)
        z_series = zscore(spread, self.window)
        z = float(z_series.iloc[-1])

        if pd.isna(z):
            return PairSignal(
                self.symbol_a, "hold", self.symbol_b, "hold", 0.0,
                "z-score not yet defined",
            )

        strength = float(min(abs(z) / 3.0, 1.0))  # z=3 → full conviction

        if z > self.entry_z:
            # Spread too wide: A overpriced relative to B → sell A, buy B
            return PairSignal(
                self.symbol_a, "sell",
                self.symbol_b, "buy",
                strength,
                f"z={z:.2f} > {self.entry_z}: sell {self.symbol_a}, buy {self.symbol_b}",
            )
        if z < -self.entry_z:
            # Spread too narrow: A underpriced relative to B → buy A, sell B
            return PairSignal(
                self.symbol_a, "buy",
                self.symbol_b, "sell",
                strength,
                f"z={z:.2f} < -{self.entry_z}: buy {self.symbol_a}, sell {self.symbol_b}",
            )
        return PairSignal(
            self.symbol_a, "hold", self.symbol_b, "hold", 0.0,
            f"z={z:.2f} within [{-self.entry_z}, {self.entry_z}] — no trade",
        )
