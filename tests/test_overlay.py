"""Tests for trader/overlay — Phase 5 Claude LLM overlay.

All tests run offline (no real Anthropic API calls). The apply_claude_overlay
function is tested by injecting a fake anthropic module via monkeypatching.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trader.strategy.base import Signal


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


def _fake_anthropic_module(response_text: str) -> ModuleType:
    """Build a fake `anthropic` module whose client returns `response_text`."""
    content_block = MagicMock()
    content_block.text = response_text

    message = MagicMock()
    message.content = [content_block]

    client = MagicMock()
    client.messages.create.return_value = message

    fake_mod = ModuleType("anthropic")
    fake_mod.Anthropic = MagicMock(return_value=client)  # type: ignore[attr-defined]
    return fake_mod


def _fake_anthropic_raises(exc: Exception) -> ModuleType:
    """Build a fake `anthropic` module whose client.messages.create raises."""
    client = MagicMock()
    client.messages.create.side_effect = exc

    fake_mod = ModuleType("anthropic")
    fake_mod.Anthropic = MagicMock(return_value=client)  # type: ignore[attr-defined]
    return fake_mod


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
    result = apply_overlay(sig, _make_bars(), config=_FakeConfig(anthropic_api_key=None))
    assert result is sig


def test_empty_api_key_passthrough():
    sig = _buy_signal()
    from trader.overlay import apply_overlay
    result = apply_overlay(sig, _make_bars(), config=_FakeConfig(anthropic_api_key=""))
    assert result is sig


# ---------------------------------------------------------------------------
# apply_claude_overlay (direct tests with mocked anthropic)
# ---------------------------------------------------------------------------

def test_approved_strength_adjusted(monkeypatch):
    from trader.overlay import claude_overlay
    payload = json.dumps({"action": "approve", "strength": 0.5, "rationale": "looks good"})
    monkeypatch.setattr(claude_overlay, "anthropic", _fake_anthropic_module(payload), raising=False)

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), "fake-key", "claude-sonnet-4-6")

    assert result.side == "buy"
    assert result.strength == pytest.approx(0.5)
    assert "overlay approved" in result.reason


def test_vetoed_becomes_hold_strength_zero(monkeypatch):
    from trader.overlay import claude_overlay
    # Claude returns a non-zero strength on veto — must be forced to 0.0
    payload = json.dumps({"action": "veto", "strength": 0.9, "rationale": "bad news incoming"})
    monkeypatch.setattr(claude_overlay, "anthropic", _fake_anthropic_module(payload), raising=False)

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), "fake-key", "claude-sonnet-4-6")

    assert result.side == "hold"
    assert result.strength == pytest.approx(0.0)
    assert "overlay veto" in result.reason


def test_api_exception_passthrough(monkeypatch):
    from trader.overlay import claude_overlay
    monkeypatch.setattr(
        claude_overlay, "anthropic",
        _fake_anthropic_raises(RuntimeError("network error")),
        raising=False,
    )

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), "fake-key", "claude-sonnet-4-6")
    assert result is sig


def test_malformed_json_passthrough(monkeypatch):
    from trader.overlay import claude_overlay
    monkeypatch.setattr(claude_overlay, "anthropic", _fake_anthropic_module("not json"), raising=False)

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), "fake-key", "claude-sonnet-4-6")
    assert result is sig


def test_strength_out_of_range_passthrough(monkeypatch):
    from trader.overlay import claude_overlay
    payload = json.dumps({"action": "approve", "strength": 1.5, "rationale": "overconfident"})
    monkeypatch.setattr(claude_overlay, "anthropic", _fake_anthropic_module(payload), raising=False)

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), "fake-key", "claude-sonnet-4-6")
    assert result is sig


def test_markdown_fences_stripped(monkeypatch):
    from trader.overlay import claude_overlay
    inner = json.dumps({"action": "approve", "strength": 0.6, "rationale": "ok"})
    wrapped = f"```json\n{inner}\n```"
    monkeypatch.setattr(claude_overlay, "anthropic", _fake_anthropic_module(wrapped), raising=False)

    sig = _buy_signal()
    result = claude_overlay.apply_claude_overlay(sig, _make_bars(), "fake-key", "claude-sonnet-4-6")
    assert result.strength == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# news_context tests
# ---------------------------------------------------------------------------

_FAKE_NEWS_HTML = """
<html><body>
<div class="SoaBEf">
  <div class="MBeuO">Apple reports record quarterly earnings</div>
</div>
<div class="SoaBEf">
  <div class="MBeuO">AAPL stock hits all-time high amid AI boom</div>
</div>
</body></html>
"""


def test_fetch_news_returns_headlines(monkeypatch):
    from unittest.mock import MagicMock
    from trader.overlay import news_context

    mock_response = MagicMock()
    mock_response.content = _FAKE_NEWS_HTML.encode()
    monkeypatch.setattr(news_context.requests, "get", lambda *a, **kw: mock_response)

    result = news_context.fetch_news("AAPL")
    assert "AAPL" in result
    assert "Apple" in result or "AAPL" in result


def test_fetch_news_request_error_returns_empty(monkeypatch):
    from trader.overlay import news_context

    monkeypatch.setattr(
        news_context.requests, "get", lambda *a, **kw: (_ for _ in ()).throw(OSError("network"))
    )

    result = news_context.fetch_news("AAPL")
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
