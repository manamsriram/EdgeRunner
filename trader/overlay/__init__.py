"""LLM overlay — Phase 5.

Non-load-bearing: any failure (missing key, API error, bad output) returns the
original signal unchanged. Claude may veto or adjust strength; never originates
a trade or flips buy↔sell.
"""
from __future__ import annotations

import os

import pandas as pd

from trader.strategy.base import Signal


def apply_fundamental_gate(
    symbol: str,
    bars: pd.DataFrame,
    config=None,
    date_str: str = "",
) -> bool:
    """Fundamental + price-trend gate for first-entry equity buys. True = approved.

    Non-load-bearing: returns True on missing key, missing financials, or any error.
    Skip for crypto — no yfinance balance sheets for BTC/USD.
    """
    if config is None or not getattr(config, "anthropic_api_key", None):
        return True

    import os
    from trader.overlay.fundamental_gate import check_fundamental_gate

    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    return check_fundamental_gate(symbol, bars, config.anthropic_api_key, model, date_str)


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
