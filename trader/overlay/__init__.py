"""LLM overlay — Phase 5 extension point.

Phase 4: pass-through stub. Phase 5 replaces apply_overlay with a Claude call
that may veto or adjust signal confidence. The pipeline never changes.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal


def apply_overlay(signal: Signal, bars: pd.DataFrame) -> Signal:
    """Return signal unchanged. Phase 5 replaces this with a non-load-bearing LLM call."""
    return signal
