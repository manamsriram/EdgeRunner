"""Tests for trader/overlay/fundamental_gate.py — first-entry fundamental gate.

All tests run offline (no real API calls). call_llm and fetch_financials are
injected via monkeypatching.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from trader.overlay import fundamental_gate
from trader.overlay.fundamental_gate import parse_fundamentals_finnhub


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
    anthropic_api_key: str | None = "fake-claude-key"
    groq_api_key: str | None = None


def _make_call_llm(response_text: str):
    return MagicMock(return_value=(response_text, None))


def _make_call_llm_raises(exc: Exception):
    return MagicMock(side_effect=exc)


FAKE_FINANCIALS = "Financials (AAPL):\nTotal Assets  100B  90B  80B\nNet Income  5B  4B  3B"

# New signature for check_fundamental_gate
_CALL_ARGS = dict(groq_key=None, groq_model="llama-3.1-8b-instant", claude_key="fake-key", claude_model="claude-haiku-4-5-20251001", date_str="2024-01-01")


def _gate(symbol="AAPL", bars=None, **kw):
    """Thin wrapper with sane defaults for the new 7-arg signature."""
    args = {**_CALL_ARGS, **kw}
    return fundamental_gate.check_fundamental_gate(
        symbol,
        bars if bars is not None else _make_bars(),
        args["groq_key"],
        args["groq_model"],
        args["claude_key"],
        args["claude_model"],
        args["date_str"],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_cache():
    fundamental_gate._clear_cache()
    yield
    fundamental_gate._clear_cache()


# ---------------------------------------------------------------------------
# Group 1: approve / veto paths
# ---------------------------------------------------------------------------

def test_approved_returns_true(monkeypatch):
    monkeypatch.setattr(fundamental_gate, "call_llm", _make_call_llm(
        json.dumps({"action": "approve", "rationale": "healthy margins"})
    ))
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert _gate() is True


def test_vetoed_returns_false(monkeypatch):
    monkeypatch.setattr(fundamental_gate, "call_llm", _make_call_llm(
        json.dumps({"action": "veto", "rationale": "negative operating income 3 years"})
    ))
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert _gate() is False


# ---------------------------------------------------------------------------
# Group 2: cache behaviour
# ---------------------------------------------------------------------------

def test_cache_prevents_second_api_call(monkeypatch):
    mock_llm = _make_call_llm(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "call_llm", mock_llm)
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        _gate(date_str="2024-01-01")
        _gate(date_str="2024-01-01")
    assert mock_llm.call_count == 1


def test_cache_miss_different_date(monkeypatch):
    mock_llm = _make_call_llm(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "call_llm", mock_llm)
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        _gate(date_str="2024-01-01")
        _gate(date_str="2024-01-02")
    assert mock_llm.call_count == 2


def test_cache_miss_different_symbol(monkeypatch):
    mock_llm = _make_call_llm(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "call_llm", mock_llm)
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        _gate(symbol="AAPL")
        _gate(symbol="MSFT")
    assert mock_llm.call_count == 2


# ---------------------------------------------------------------------------
# Group 3: failure / pass-through (non-load-bearing)
# ---------------------------------------------------------------------------

def test_api_exception_returns_true(monkeypatch):
    monkeypatch.setattr(fundamental_gate, "call_llm", _make_call_llm_raises(RuntimeError("timeout")))
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert _gate() is True


def test_malformed_json_returns_true(monkeypatch):
    monkeypatch.setattr(fundamental_gate, "call_llm", _make_call_llm("not valid json at all"))
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert _gate() is True


def test_invalid_action_returns_true(monkeypatch):
    monkeypatch.setattr(fundamental_gate, "call_llm", _make_call_llm(
        json.dumps({"action": "maybe", "rationale": "uncertain"})
    ))
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert _gate() is True


def test_no_llm_response_returns_true(monkeypatch):
    """Empty string from call_llm (both providers absent/failed) → approve."""
    monkeypatch.setattr(fundamental_gate, "call_llm", _make_call_llm(""))
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert _gate() is True


def test_empty_financials_approve_no_api_call(monkeypatch):
    mock_llm = _make_call_llm(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "call_llm", mock_llm)
    with patch.object(fundamental_gate, "fetch_financials", return_value=""):
        result = _gate()
    assert result is True
    assert mock_llm.call_count == 0


def test_empty_financials_not_cached(monkeypatch):
    """Empty financials must not populate the cache — next tick should retry the fetch."""
    mock_llm = _make_call_llm(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "call_llm", mock_llm)
    with patch.object(fundamental_gate, "fetch_financials", return_value=""):
        _gate()
        _gate()
    assert mock_llm.call_count == 0


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
        groq_api_key: str | None = None

    assert apply_fundamental_gate("AAPL", _make_bars(), config=_NoKey(), date_str="2024-01-01") is True


# ---------------------------------------------------------------------------
# Group 5: fence stripping and prompt content
# ---------------------------------------------------------------------------

def test_markdown_fences_stripped(monkeypatch):
    fenced = "```json\n" + json.dumps({"action": "approve", "rationale": "ok"}) + "\n```"
    monkeypatch.setattr(fundamental_gate, "call_llm", _make_call_llm(fenced))
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        assert _gate() is True


def test_price_context_included_in_prompt(monkeypatch):
    mock_llm = _make_call_llm(json.dumps({"action": "approve", "rationale": "ok"}))
    monkeypatch.setattr(fundamental_gate, "call_llm", mock_llm)
    with patch.object(fundamental_gate, "fetch_financials", return_value=FAKE_FINANCIALS):
        _gate(bars=_make_bars(n=30))

    # call_llm(system, user_message, max_tokens, ...)
    user_message = mock_llm.call_args[0][1]
    assert "Window high" in user_message or "MA20" in user_message or "Current price" in user_message


# ---------------------------------------------------------------------------
# Group 6: parse_fundamentals_finnhub
# ---------------------------------------------------------------------------

def test_parse_fundamentals_finnhub_extracts_floats():
    metrics = {
        "peBasicExclExtraTTM": 22.5,
        "currentEv/freeCashFlowTTM": 18.0,
        "grossMarginTTM": 42.3,
        "revenueGrowthTTMYoy": 12.1,
    }
    recs = [{"buy": 10, "hold": 4, "sell": 1, "period": "2026-06"}]
    parsed = parse_fundamentals_finnhub(metrics, recs)
    assert parsed["pe_ttm"] == 22.5
    assert parsed["ev_fcf_ttm"] == 18.0
    assert parsed["gross_margin_ttm"] == 42.3
    assert parsed["revenue_growth_yoy"] == 12.1
    assert parsed["analyst_buy_count"] == 10.0
    assert parsed["analyst_hold_count"] == 4.0
    assert parsed["analyst_sell_count"] == 1.0


def test_parse_fundamentals_finnhub_missing_fields_omitted():
    parsed = parse_fundamentals_finnhub({}, [])
    assert parsed == {}
