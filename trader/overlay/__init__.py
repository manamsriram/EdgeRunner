"""LLM overlay — Phase 5.

Non-load-bearing: any failure (missing key, API error, bad output) returns the
original signal unchanged. Claude may veto or adjust strength; never originates
a trade or flips buy↔sell.
"""
from __future__ import annotations

import os

import pandas as pd

from trader.strategy.base import Signal


def apply_overlay(signal: Signal, bars: pd.DataFrame, config=None) -> Signal:
    """Apply Claude overlay when an API key is configured; otherwise pass through.

    Invariants preserved by the overlay implementation:
      - Signal.side remains one of {"buy", "sell", "hold"}.
      - Signal.strength remains within [0.0, 1.0].
    """
    if config is None or not getattr(config, "anthropic_api_key", None):
        return signal

    from trader.overlay.claude_overlay import apply_claude_overlay

    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    return apply_claude_overlay(signal, bars, config.anthropic_api_key, model)
