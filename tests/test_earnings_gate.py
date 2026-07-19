"""Tests for trader/overlay/earnings_gate.py — hard earnings-calendar gate.

Deterministic, no LLM. All tests run offline via a fake finnhub_client.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trader.overlay import earnings_gate


@pytest.fixture(autouse=True)
def clear_cache():
    earnings_gate._clear_cache()
    yield
    earnings_gate._clear_cache()


def _fake_client(entries):
    client = MagicMock()
    client.earnings_calendar.return_value = entries
    return client


def test_no_earnings_in_window_approves():
    client = _fake_client([])
    assert earnings_gate.check_earnings_gate("AAPL", client, "2024-01-02") is True


def test_earnings_in_window_vetoes():
    client = _fake_client([{"date": "2024-01-03", "symbol": "AAPL"}])
    assert earnings_gate.check_earnings_gate("AAPL", client, "2024-01-02") is False


def test_no_client_approves():
    assert earnings_gate.check_earnings_gate("AAPL", None, "2024-01-02") is True


def test_no_date_str_approves():
    client = _fake_client([{"date": "2024-01-03"}])
    assert earnings_gate.check_earnings_gate("AAPL", client, "") is True


def test_client_exception_approves():
    client = MagicMock()
    client.earnings_calendar.side_effect = RuntimeError("timeout")
    assert earnings_gate.check_earnings_gate("AAPL", client, "2024-01-02") is True


def test_cache_prevents_second_api_call():
    client = _fake_client([])
    earnings_gate.check_earnings_gate("AAPL", client, "2024-01-02")
    earnings_gate.check_earnings_gate("AAPL", client, "2024-01-02")
    assert client.earnings_calendar.call_count == 1


def test_cache_miss_different_date():
    client = _fake_client([])
    earnings_gate.check_earnings_gate("AAPL", client, "2024-01-02")
    earnings_gate.check_earnings_gate("AAPL", client, "2024-01-03")
    assert client.earnings_calendar.call_count == 2


def test_cache_miss_different_symbol():
    client = _fake_client([])
    earnings_gate.check_earnings_gate("AAPL", client, "2024-01-02")
    earnings_gate.check_earnings_gate("MSFT", client, "2024-01-02")
    assert client.earnings_calendar.call_count == 2


def test_apply_earnings_gate_no_config_approves():
    from trader.overlay import apply_earnings_gate
    assert apply_earnings_gate("AAPL", config=None, date_str="2024-01-02") is True


def test_apply_earnings_gate_no_finnhub_key_approves():
    from dataclasses import dataclass
    from trader.overlay import apply_earnings_gate

    @dataclass(frozen=True)
    class _NoKey:
        finnhub_api_key: str | None = None

    assert apply_earnings_gate("AAPL", config=_NoKey(), date_str="2024-01-02") is True
