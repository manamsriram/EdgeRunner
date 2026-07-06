"""Estimate $ cost of an LLM call from real provider-reported token usage.

Phase 0 of the ML-overlay research plan: measure actual overlay spend before
deciding whether replacing it is worth the engineering in later phases.
"""
from __future__ import annotations

from trader.overlay.llm_client import LLMUsage

# $ per 1M tokens (input, output). Update if models/pricing change — this is a
# point-in-time estimate, not billed truth (no discounts, batching, or caching
# credits applied).
_PRICING_PER_1M: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


def estimate_cost_usd(usage: LLMUsage | None) -> float:
    """Return estimated $ cost for one call; 0.0 for a cache hit or missing usage."""
    if usage is None:
        return 0.0
    input_price, output_price = _PRICING_PER_1M.get(usage.model, (0.0, 0.0))
    return (usage.input_tokens * input_price + usage.output_tokens * output_price) / 1_000_000
