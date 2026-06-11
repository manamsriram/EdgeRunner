"""LLM overlay — Phase 5.

Non-load-bearing: any failure (missing key, API error, bad output) returns the
original signal unchanged. LLM may veto or adjust strength; never originates
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

    Non-load-bearing: returns True on missing keys, missing financials, or any error.
    Skip for crypto — no yfinance balance sheets for BTC/USD.
    """
    if config is None:
        return True
    groq_key = getattr(config, "groq_api_key", None)
    claude_key = getattr(config, "anthropic_api_key", None)
    gemini_key = getattr(config, "gemini_api_key", None)
    if not gemini_key and not groq_key and not claude_key:
        return True

    from trader.overlay.fundamental_gate import check_fundamental_gate

    groq_model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    claude_model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")
    return check_fundamental_gate(
        symbol, bars, groq_key, groq_model, claude_key, claude_model, date_str,
        gemini_key=gemini_key, gemini_model=gemini_model,
    )


def apply_overlay(signal: Signal, bars: pd.DataFrame, config=None) -> Signal:
    """Apply LLM overlay when at least one API key is configured; otherwise pass through.

    Invariants preserved by the overlay implementation:
      - Signal.side remains one of {"buy", "sell", "hold"}.
      - Signal.strength remains within [0.0, 1.0].
    """
    if config is None:
        return signal
    groq_key = getattr(config, "groq_api_key", None)
    claude_key = getattr(config, "anthropic_api_key", None)
    gemini_key = getattr(config, "gemini_api_key", None)
    if not gemini_key and not groq_key and not claude_key:
        return signal

    from trader.overlay.claude_overlay import apply_claude_overlay

    groq_model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    claude_model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")
    return apply_claude_overlay(
        signal, bars, groq_key, groq_model, claude_key, claude_model,
        gemini_key=gemini_key, gemini_model=gemini_model,
    )
