"""LLM overlay — Phase 5 extension point.

Phase 4: pass-through stub. Phase 5 replaces apply_overlay with a Claude call
that may veto or adjust signal strength (Signal.strength, a float in [0.0, 1.0]).
The pipeline never changes.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal


def apply_overlay(signal: Signal, bars: pd.DataFrame) -> Signal:
    """Return signal unchanged (Phase 4 pass-through).

    Phase 5 invariants that the LLM-based replacement MUST preserve:
      - Signal.side must remain one of {"buy", "sell", "hold"}.
      - Signal.strength must remain within [0.0, 1.0].

    Validate post-LLM and raise ValueError if either invariant is violated.
    """
    return signal
