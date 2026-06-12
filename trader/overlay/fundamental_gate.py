"""First-entry fundamental gate — Phase 5.5.

Non-load-bearing: any failure (missing key, API error, empty financials) returns True
(approve). Only runs on the first buy entry for a symbol (no existing position). Skipped
for crypto — yfinance has no balance sheets for BTC/USD.

Results cached in-process by (symbol, date_str) — runs at most once per symbol per day.
Empty financials are NOT cached so the next tick retries the fetch.
"""
from __future__ import annotations

import json
import logging
import re

import pandas as pd

from trader.overlay.llm_client import call_llm
from trader.overlay.news_context import fetch_financials

logger = logging.getLogger(__name__)

# ---- Cache ----

# (symbol, date_str) -> {"action": "approve"|"veto", "rationale": str}
_FUNDAMENTAL_CACHE: dict[tuple[str, str], dict] = {}


def _clear_cache() -> None:
    """Test helper — clears the in-process fundamental cache."""
    _FUNDAMENTAL_CACHE.clear()


# ---- System prompt ----

_SYSTEM_PROMPT = """\
You are a senior fundamental analyst at a systematic trading fund. You review the first
purchase entry for a US equity or ETF to determine whether the company's financial health
justifies a new long position. You are reviewing fundamental fitness only — not the specific
trade signal.

The price context is provided for background only — use it to distinguish whether a price
decline reflects deteriorating fundamentals vs. normal market volatility. Do NOT veto solely
because price is near its window low; the strategy deliberately targets oversold conditions.

VETO the entry (action="veto") ONLY on clear fundamental distress:
- Negative or sharply deteriorating operating margin across all reported periods
  (e.g. operating income negative for 2+ consecutive years, or margin declining faster
  than 5 percentage points per year with no reversal)
- Debt/Equity ratio above 5.0 AND interest coverage ratio below 1.5x
- Total revenue declining more than 15% YoY in the most recent period AND declining
  in the prior period (consecutive revenue contraction)
- Negative working capital by more than 20% (current liabilities exceed current assets)
- Net income negative for all reported periods with no trajectory improvement
- Volume collapse confirmed by financials showing liquidity/going-concern risk (not just
  low trading volume — use the volume trend as corroborating context only)

APPROVE (action="approve") when:
- Financial data is missing, partial, or ambiguous — default bias is APPROVE
- Metrics are mixed without a single clear deterioration
- ETFs and index funds (SPY, VOO, QQQ, etc.) — always approve
- Growth-stage issuer: negative net income but improving gross margin and revenue growth
- Price is near window lows but fundamentals are intact — expected for mean-reversion
  strategies, not a red flag

Reserve veto for clear, multi-metric distress. False veto costs edge permanently; missed
veto costs at most one trade — the risk gate still enforces position limits.

NEVER flip buy to sell. NEVER originate trades. NEVER comment on momentum signals.

Respond ONLY with valid JSON — no markdown, no text outside the JSON object:
{"action": "approve"|"veto", "rationale": "<one sentence citing the specific metric>"}"""


# ---- Helpers ----

def _price_context(symbol: str, bars: pd.DataFrame) -> str:
    """Derive price trend metrics from the already-fetched bars DataFrame."""
    if bars is None or bars.empty or len(bars) < 2:
        return f"Price data unavailable for {symbol}"

    closes = bars["close"].dropna()
    volumes = bars["volume"].dropna()

    if closes.empty:
        return f"Price data unavailable for {symbol}"

    current = float(closes.iloc[-1])
    high_w = float(closes.max())
    low_w = float(closes.min())
    pct_from_high = (current - high_w) / high_w * 100
    pct_from_low = (current - low_w) / low_w * 100

    ma20 = float(closes.iloc[-20:].mean()) if len(closes) >= 20 else None
    ma_full = float(closes.mean())
    trend = f"MA20/MA_full = {ma20 / ma_full:.3f}" if ma20 else "insufficient bars for MA20"

    vol_avg = float(volumes.mean()) if not volumes.empty else 0.0
    vol_recent = float(volumes.iloc[-20:].mean()) if len(volumes) >= 20 else None
    if vol_recent and vol_avg > 0:
        vol_note = f"recent 20d avg volume {vol_recent / vol_avg:.2f}x full-window avg"
    else:
        vol_note = "insufficient data for volume trend"

    return (
        f"Price context ({symbol}, {len(closes)} trading days):\n"
        f"- Current price: {current:.2f}\n"
        f"- Window high: {high_w:.2f} ({pct_from_high:+.1f}% from high)\n"
        f"- Window low: {low_w:.2f} ({pct_from_low:+.1f}% from low)\n"
        f"- Momentum: {trend}\n"
        f"- Volume: {vol_note}"
    )


def _parse_response(text: str) -> dict:
    """Parse LLM JSON response, stripping markdown fences if present."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


# ---- Core gate ----

def check_fundamental_gate(
    symbol: str,
    bars: pd.DataFrame,
    groq_key: str | None,
    groq_model: str,
    claude_key: str | None,
    claude_model: str,
    date_str: str,
    gemini_key: str | None = None,
    gemini_model: str = "gemini-3.1-flash-lite",
) -> bool:
    """Run fundamental + price-trend check for a first-entry equity buy.

    Returns True (approved) or False (vetoed). Non-load-bearing: returns True on any
    failure. Caches the verdict by (symbol, date_str) — one API call per symbol per
    trading day. Empty financials are not cached so the next tick retries the fetch.
    """
    try:
        cache_key = (symbol, date_str)
        if cache_key in _FUNDAMENTAL_CACHE:
            cached = _FUNDAMENTAL_CACHE[cache_key]
            logger.debug("fundamental gate cache hit symbol=%s date=%s action=%s", symbol, date_str, cached["action"])
            return cached["action"] == "approve"

        financials = fetch_financials(symbol)
        if not financials:
            logger.debug("fundamental gate no financials for %s — approving", symbol)
            return True  # not cached — retry fetch next tick

        price_ctx = _price_context(symbol, bars)
        user_message = f"{price_ctx}\n\nFinancial statements:\n{financials}"

        logger.info("fundamental gate request symbol=%s date=%s", symbol, date_str)

        raw = call_llm(_SYSTEM_PROMPT, user_message, 128, groq_key, groq_model, claude_key, claude_model, gemini_key, gemini_model)
        if not raw:
            logger.warning("fundamental gate: no LLM response for %s, approving", symbol)
            return True

        parsed = _parse_response(raw)
        action = parsed["action"]
        rationale = parsed.get("rationale", "")

        if action not in {"approve", "veto"}:
            raise ValueError(f"unexpected action {action!r}")

        _FUNDAMENTAL_CACHE[cache_key] = {"action": action, "rationale": rationale}
        logger.info(
            "fundamental gate response symbol=%s action=%s rationale=%r",
            symbol, action, rationale,
        )
        return action == "approve"

    except Exception as exc:
        logger.warning("fundamental gate failed for %s, approving: %s", symbol, exc)
        return True
