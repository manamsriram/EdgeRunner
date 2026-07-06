"""Tests for trader/overlay/cost_tracking.py — Phase 0 LLM cost estimation."""
from __future__ import annotations

from trader.overlay.cost_tracking import estimate_cost_usd
from trader.overlay.llm_client import LLMUsage


def test_none_usage_is_free():
    assert estimate_cost_usd(None) == 0.0


def test_known_model_priced():
    usage = LLMUsage("gemini", "gemini-3.1-flash-lite", input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost_usd(usage) == 0.10 + 0.40


def test_unknown_model_prices_zero():
    usage = LLMUsage("mystery", "mystery-model-v9", input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost_usd(usage) == 0.0


def test_scales_linearly_with_tokens():
    usage = LLMUsage("groq", "llama-3.1-8b-instant", input_tokens=500_000, output_tokens=0)
    assert estimate_cost_usd(usage) == 0.025
