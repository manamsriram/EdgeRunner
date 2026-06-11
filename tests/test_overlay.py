"""Tests for trader/overlay — Phase 5 LLM overlay (Groq primary, Claude fallback).

All tests run offline (no real API calls). apply_claude_overlay is tested by
monkeypatching call_llm. fetch_news is tested by monkeypatching NewsClient.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pandas as pd
import pytest

from trader.strategy.base import Signal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_overlay_cache():
    from trader.overlay.claude_overlay import _clear_overlay_cache
    _clear_overlay_cache()
    yield
    _clear_overlay_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bars(n: int = 30) -> pd.DataFrame:
    import numpy as np
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    prices = 100 + np.linspace(0, 10, n)
    return pd.DataFrame(
        {"open": prices - 0.5, "high": prices + 1, "low": prices - 1,
         "close": prices, "volume": 1_000_000},
        index=idx,
    )


def _buy_signal() -> Signal:
    return Signal(symbol="AAPL", side="buy", strength=0.7, reason="momentum crossover")


@dataclass(frozen=True)
class _FakeConfig:
    anthropic_api_key: str | None = None
    groq_api_key: str | None = None


def _make_call_llm(response_text: str):
    """Return a call_llm replacement that always returns response_text."""
    return lambda *a, **kw: response_text


def _make_call_llm_raises(exc: Exception):
    """Return a call_llm replacement that always raises exc."""
    def _raise(*a, **kw):
        raise exc
    return _raise


# ---------------------------------------------------------------------------
# apply_overlay (public entry point)
# ---------------------------------------------------------------------------

def test_no_config_passthrough():
    sig = _buy_signal()
    from trader.overlay import apply_overlay
    result = apply_overlay(sig, _make_bars())
    assert result is sig


def test_no_api_key_passthrough():
    sig = _buy_signal()
    from trader.overlay import apply_overlay
    result = apply_overlay(sig, _make_bars(), config=_FakeConfig(anthropic_api_key=None, groq_api_key=None))
    assert result is sig


def test_empty_api_key_passthrough():
    sig = _buy_signal()
    from trader.overlay import apply_overlay
    result = apply_overlay(sig, _make_bars(), config=_FakeConfig(anthropic_api_key="", groq_api_key=""))
    assert result is sig


# ---------------------------------------------------------------------------
# apply_claude_overlay (direct tests with mocked call_llm)
# ---------------------------------------------------------------------------

def test_approved_strength_adjusted(monkeypatch):
    from trader.overlay import claude_overlay
    payload = json.dumps({"action": "approve", "strength": 0.5, "rationale": "looks good"})
    monkeypatch.setattr(claude_overlay, "call_llm", _make_call_llm(payload))

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), None, "llama-3.1-8b-instant", "fake-key", "claude-haiku-4-5-20251001")

    assert result.side == "buy"
    assert result.strength == pytest.approx(0.5)
    assert "overlay approved" in result.reason


def test_vetoed_becomes_hold_strength_zero(monkeypatch):
    from trader.overlay import claude_overlay
    payload = json.dumps({"action": "veto", "strength": 0.9, "rationale": "bad news incoming"})
    monkeypatch.setattr(claude_overlay, "call_llm", _make_call_llm(payload))

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), None, "llama-3.1-8b-instant", "fake-key", "claude-haiku-4-5-20251001")

    assert result.side == "hold"
    assert result.strength == pytest.approx(0.0)
    assert "overlay veto" in result.reason


def test_api_exception_passthrough(monkeypatch):
    from trader.overlay import claude_overlay
    monkeypatch.setattr(claude_overlay, "call_llm", _make_call_llm_raises(RuntimeError("network error")))

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), None, "llama-3.1-8b-instant", "fake-key", "claude-haiku-4-5-20251001")
    assert result is sig


def test_malformed_json_passthrough(monkeypatch):
    from trader.overlay import claude_overlay
    monkeypatch.setattr(claude_overlay, "call_llm", _make_call_llm("not json"))

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), None, "llama-3.1-8b-instant", "fake-key", "claude-haiku-4-5-20251001")
    assert result is sig


def test_strength_out_of_range_passthrough(monkeypatch):
    from trader.overlay import claude_overlay
    payload = json.dumps({"action": "approve", "strength": 1.5, "rationale": "overconfident"})
    monkeypatch.setattr(claude_overlay, "call_llm", _make_call_llm(payload))

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), None, "llama-3.1-8b-instant", "fake-key", "claude-haiku-4-5-20251001")
    assert result is sig


def test_no_llm_response_passthrough(monkeypatch):
    """Empty string from call_llm (both providers absent/failed) → pass through."""
    from trader.overlay import claude_overlay
    monkeypatch.setattr(claude_overlay, "call_llm", _make_call_llm(""))

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), None, "llama-3.1-8b-instant", "fake-key", "claude-haiku-4-5-20251001")
    assert result is sig


def test_markdown_fences_stripped(monkeypatch):
    from trader.overlay import claude_overlay
    inner = json.dumps({"action": "approve", "strength": 0.6, "rationale": "ok"})
    wrapped = f"```json\n{inner}\n```"
    monkeypatch.setattr(claude_overlay, "call_llm", _make_call_llm(wrapped))

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), None, "llama-3.1-8b-instant", "fake-key", "claude-haiku-4-5-20251001")
    assert result.strength == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# news_context tests
# ---------------------------------------------------------------------------

def test_fetch_news_returns_headlines(monkeypatch):
    from unittest.mock import MagicMock
    from trader.overlay import news_context
    import alpaca.data.historical.news as alpaca_news_mod

    mock_article = MagicMock()
    mock_article.headline = "Apple reports record quarterly earnings"
    mock_response = MagicMock()
    mock_response.news = [mock_article]

    mock_client = MagicMock()
    mock_client.get_news.return_value = mock_response

    monkeypatch.setattr(alpaca_news_mod, "NewsClient", MagicMock(return_value=mock_client))

    result = news_context.fetch_news("AAPL", alpaca_api_key="key", alpaca_secret_key="secret")
    assert "AAPL" in result
    assert "Apple" in result


def test_fetch_news_error_returns_empty(monkeypatch):
    from unittest.mock import MagicMock
    from trader.overlay import news_context
    import alpaca.data.historical.news as alpaca_news_mod

    mock_client = MagicMock()
    mock_client.get_news.side_effect = OSError("network")
    monkeypatch.setattr(alpaca_news_mod, "NewsClient", MagicMock(return_value=mock_client))

    result = news_context.fetch_news("AAPL", alpaca_api_key="key", alpaca_secret_key="secret")
    assert result == ""


def test_fetch_financials_returns_string(monkeypatch):
    import pandas as pd
    from unittest.mock import MagicMock
    from trader.overlay import news_context

    mock_ticker = MagicMock()
    mock_ticker.balance_sheet = pd.DataFrame(
        {"2024": [1e9, 2e9], "2023": [0.9e9, 1.8e9]},
        index=["TotalAssets", "TotalLiabilities"],
    )
    mock_ticker.income_stmt = pd.DataFrame()
    monkeypatch.setattr(news_context.yf, "Ticker", lambda s: mock_ticker)

    result = news_context.fetch_financials("AAPL")
    assert "AAPL" in result
    assert "TotalAssets" in result


def test_fetch_financials_error_returns_empty(monkeypatch):
    from trader.overlay import news_context

    monkeypatch.setattr(news_context.yf, "Ticker", lambda s: (_ for _ in ()).throw(RuntimeError("fail")))

    result = news_context.fetch_financials("AAPL")
    assert result == ""
