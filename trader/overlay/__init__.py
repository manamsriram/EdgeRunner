"""LLM overlay — Phase 5.

Non-load-bearing: any failure (missing key, API error, bad output) returns the
original signal unchanged. LLM may veto or adjust strength; never originates
a trade or flips buy↔sell.
"""
from __future__ import annotations

import os
import threading

import pandas as pd

from trader.strategy.base import Signal

# ---- Finnhub client singleton ----

_finnhub_client = None
_finnhub_lock = threading.Lock()


def _reset_finnhub_client() -> None:
    """Test helper — resets the Finnhub client singleton."""
    global _finnhub_client
    with _finnhub_lock:
        _finnhub_client = None


def _get_finnhub_client(config):
    global _finnhub_client
    key = getattr(config, "finnhub_api_key", None)
    if key and _finnhub_client is None:
        with _finnhub_lock:
            if _finnhub_client is None:
                from trader.data.finnhub_client import FinnhubClient
                _finnhub_client = FinnhubClient(key)
    return _finnhub_client if key else None


# ---- Sentiment client singleton ----

_sentiment_client = None


def _reset_sentiment_client() -> None:
    """Test helper."""
    global _sentiment_client
    _sentiment_client = None


def _get_sentiment_client(config, finnhub_client):
    global _sentiment_client
    rid = getattr(config, "reddit_client_id", None)
    rsecret = getattr(config, "reddit_client_secret", None)
    if _sentiment_client is None and (rid or finnhub_client is not None):
        from trader.data.sentiment_client import SentimentClient
        _sentiment_client = SentimentClient(
            finnhub_client=finnhub_client,
            reddit_client_id=rid,
            reddit_client_secret=rsecret,
        )
    return _sentiment_client


def apply_fundamental_gate(
    symbol: str,
    bars: pd.DataFrame,
    config=None,
    date_str: str = "",
    repo=None,
) -> bool:
    """Fundamental + price-trend gate for first-entry equity buys. True = approved.

    Non-load-bearing: returns True on missing keys, missing financials, or any error.
    Skip for crypto — no yfinance balance sheets for BTC/USD.

    `repo` is optional — when set, each LLM call (or cache hit) is logged to
    llm_call_log for cost measurement.
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
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    finnhub_client = _get_finnhub_client(config)
    return check_fundamental_gate(
        symbol, bars, groq_key, groq_model, claude_key, claude_model, date_str,
        gemini_key=gemini_key, gemini_model=gemini_model,
        finnhub_client=finnhub_client, repo=repo,
    )


def apply_earnings_gate(
    symbol: str,
    config=None,
    date_str: str = "",
) -> bool:
    """Hard earnings-calendar gate for equity buys. True = approved.

    Non-load-bearing: returns True when Finnhub isn't configured or the
    calendar lookup fails. Skip for crypto — no earnings events.
    """
    if config is None:
        return True
    finnhub_client = _get_finnhub_client(config)
    if finnhub_client is None:
        return True

    from trader.overlay.earnings_gate import check_earnings_gate

    days_ahead = int(os.getenv("EARNINGS_GATE_DAYS_AHEAD", "2"))
    return check_earnings_gate(symbol, finnhub_client, date_str, days_ahead=days_ahead)


def apply_overlay(
    signal: Signal, bars: pd.DataFrame, config=None,
    repo=None, strategy_name: str | None = None, regime: str | None = None,
) -> Signal:
    """Apply LLM overlay when at least one API key is configured; otherwise pass through.

    Invariants preserved by the overlay implementation:
      - Signal.side remains one of {"buy", "sell", "hold"}.
      - Signal.strength remains within [0.0, 1.0].

    `repo`/`strategy_name`/`regime` are optional — when `repo` is set and
    `config.risk.trade_memory_shadow`/`_live` is on, recent trade outcomes for this
    symbol are looked up and (in live mode) injected into the overlay prompt.
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
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    finnhub_client = _get_finnhub_client(config)
    sentiment_client = _get_sentiment_client(config, finnhub_client)
    return apply_claude_overlay(
        signal, bars, groq_key, groq_model, claude_key, claude_model,
        gemini_key=gemini_key, gemini_model=gemini_model,
        config=config,
        sentiment_client=sentiment_client,
        repo=repo, strategy_name=strategy_name, regime=regime,
    )
