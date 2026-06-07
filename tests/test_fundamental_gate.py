"""Tests for trader/overlay/fundamental_gate.py — first-entry fundamental gate.

All tests run offline (no real Anthropic or yfinance calls). The anthropic module and
fetch_financials are injected via monkeypatching, matching the pattern in test_overlay.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from types import ModuleType
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from trader.overlay import fundamental_gate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bars(n: int = 30) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    prices = 100 + np.linspace(0, 10, n)
    return pd.DataFrame(
        {"open": prices - 0.5, "high": prices + 1, "low": prices - 1,
         "close": prices, "volume": 1_000_000},
        index=idx,
    )


@dataclass(frozen=True)
class _FakeConfig:
    anthropic_api_key: str | None = "fake-key"


def _fake_anthropic_module(response_text: str) -> ModuleType:
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
    client = MagicMock()
    client.messages.create.side_effect = exc

    fake_mod = ModuleType("anthropic")
    fake_mod.Anthropic = MagicMock(return_value=client)  # type: ignore[attr-defined]
    return fake_mod


FAKE_FINANCIALS = "Financials (AAPL):\nTotal Assets  100B  90B  80B\nNet Income  5B  4B  3B"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_cache():
    """Reset module-level cache before and after every test."""
    fundamental_gate._clear_cache()
    yield
    fundamental_gate._clear_cache()


# ---------------------------------------------------------------------------
# Group 1: approve / veto paths
# ---------------------------------------------------------------------------

def test_approved_returns_true(monkeypatch):
    fake_mod = _fake_anthropic_module(json.dumps({"action": "approve", "rationale": "healthy margins"}))
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        result = fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01")
    assert result is True


def test_vetoed_returns_false(monkeypatch):
    fake_mod = _fake_anthropic_module(json.dumps({"action": "veto", "rationale": "negative operating income 3 years"}))
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        result = fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01")
    assert result is False


# ---------------------------------------------------------------------------
# Group 2: cache behaviour
# ---------------------------------------------------------------------------

def test_cache_prevents_second_api_call(monkeypatch):
    fake_mod = _fake_anthropic_module(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    client = fake_mod.Anthropic.return_value

    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01")
        fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01")

    assert client.messages.create.call_count == 1


def test_cache_miss_different_date(monkeypatch):
    fake_mod = _fake_anthropic_module(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    client = fake_mod.Anthropic.return_value

    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01")
        fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-02")

    assert client.messages.create.call_count == 2


def test_cache_miss_different_symbol(monkeypatch):
    fake_mod = _fake_anthropic_module(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    client = fake_mod.Anthropic.return_value

    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01")
        fundamental_gate.check_fundamental_gate("MSFT", _make_bars(), "key", "model", "2024-01-01")

    assert client.messages.create.call_count == 2


# ---------------------------------------------------------------------------
# Group 3: failure / pass-through (non-load-bearing)
# ---------------------------------------------------------------------------

def test_api_exception_returns_true(monkeypatch):
    fake_mod = _fake_anthropic_raises(RuntimeError("timeout"))
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01") is True


def test_malformed_json_returns_true(monkeypatch):
    fake_mod = _fake_anthropic_module("not valid json at all")
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01") is True


def test_invalid_action_returns_true(monkeypatch):
    fake_mod = _fake_anthropic_module(json.dumps({"action": "maybe", "rationale": "uncertain"}))
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01") is True


def test_missing_anthropic_returns_true(monkeypatch):
    monkeypatch.setattr(fundamental_gate, "anthropic", None)
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01") is True


def test_empty_financials_approve_no_api_call(monkeypatch):
    fake_mod = _fake_anthropic_module(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    client = fake_mod.Anthropic.return_value

    with patch.object(fundamental_gate, "fetch_financials", return_value=""):
        result = fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01")

    assert result is True
    assert client.messages.create.call_count == 0


def test_empty_financials_not_cached(monkeypatch):
    """Empty financials must not populate the cache — next tick should retry the fetch."""
    fake_mod = _fake_anthropic_module(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    client = fake_mod.Anthropic.return_value

    with patch.object(fundamental_gate, "fetch_financials", return_value=""):
        fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01")
        fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01")

    assert client.messages.create.call_count == 0


# ---------------------------------------------------------------------------
# Group 4: apply_fundamental_gate public entry point
# ---------------------------------------------------------------------------

def test_no_api_key_returns_true_no_import():
    from trader.overlay import apply_fundamental_gate
    assert apply_fundamental_gate("AAPL", _make_bars(), config=None, date_str="2024-01-01") is True


def test_no_api_key_on_config_returns_true():
    from trader.overlay import apply_fundamental_gate

    @dataclass(frozen=True)
    class _NoKey:
        anthropic_api_key: str | None = None

    assert apply_fundamental_gate("AAPL", _make_bars(), config=_NoKey(), date_str="2024-01-01") is True


# ---------------------------------------------------------------------------
# Group 5: fence stripping and prompt content
# ---------------------------------------------------------------------------

def test_markdown_fences_stripped(monkeypatch):
    fenced = "```json\n" + json.dumps({"action": "approve", "rationale": "ok"}) + "\n```"
    fake_mod = _fake_anthropic_module(fenced)
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert fundamental_gate.check_fundamental_gate("AAPL", _make_bars(), "key", "model", "2024-01-01") is True


def test_price_context_included_in_prompt(monkeypatch):
    fake_mod = _fake_anthropic_module(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "anthropic", fake_mod)
    client = fake_mod.Anthropic.return_value

    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        fundamental_gate.check_fundamental_gate("AAPL", _make_bars(n=30), "key", "model", "2024-01-01")

    call_kwargs = client.messages.create.call_args
    user_content = call_kwargs[1]["messages"][0]["content"]
    assert "Window high" in user_content or "MA20" in user_content or "Current price" in user_content
