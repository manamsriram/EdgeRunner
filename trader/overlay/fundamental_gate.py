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

from trader.overlay.cost_tracking import estimate_cost_usd
from trader.overlay.llm_client import call_llm
from trader.overlay.news_context import fetch_financials

logger = logging.getLogger(__name__)

# ---- Cache ----

# (symbol, date_str) -> {"action": "approve"|"veto", "rationale": str}
_FUNDAMENTAL_CACHE: dict[tuple[str, str], dict] = {}


def _clear_cache() -> None:
    """Test helper — clears the in-process fundamental cache."""
    _FUNDAMENTAL_CACHE.clear()


def _log_llm_call(repo, provider: str, symbol: str, cache_hit: bool, usage) -> None:
    """Best-effort cost-log write. Never raises — logging must not affect the gate."""
    if repo is None:
        return
    try:
        repo.record_llm_call(
            provider=provider,
            call_site="fundamental_gate",
            symbol=symbol,
            cache_hit=cache_hit,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            est_cost_usd=estimate_cost_usd(usage),
        )
    except Exception:
        logger.warning("llm call-log write failed for %s", symbol, exc_info=True)


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

def fetch_fundamentals_finnhub(symbol: str, client) -> str:
    """Fetch structured fundamentals from Finnhub. Returns '' on any failure."""
    try:
        metrics = client.basic_financials(symbol)
        recs = client.recommendation_trends(symbol)
        if not metrics:
            return ""

        lines = [f"Fundamentals ({symbol}, via Finnhub):"]

        pe = metrics.get("peBasicExclExtraTTM")
        if pe is None:
            pe = metrics.get("peTTM")
        if pe is not None:
            lines.append(f"- P/E (TTM): {pe:.1f}")

        ev_ebitda = metrics.get("currentEv/freeCashFlowTTM")
        if ev_ebitda is not None:
            lines.append(f"- EV/FCF (TTM): {ev_ebitda:.1f}")

        gross_margin = metrics.get("grossMarginTTM")
        if gross_margin is not None:
            lines.append(f"- Gross Margin (TTM): {gross_margin:.1f}%")

        rev_growth = metrics.get("revenueGrowthTTMYoy")
        if rev_growth is not None:
            lines.append(f"- Revenue Growth YoY: {rev_growth:.1f}%")

        if recs:
            latest = recs[0]
            buy = latest.get("buy", 0)
            hold = latest.get("hold", 0)
            sell = latest.get("sell", 0)
            period = latest.get("period", "")
            lines.append(f"- Analyst consensus ({period}): {buy} buy, {hold} hold, {sell} sell")

        return "\n".join(lines)
    except Exception:
        return ""


def parse_fundamentals_finnhub(metrics: dict, recs: list[dict]) -> dict[str, float]:
    """Extract the same fields fetch_fundamentals_finnhub formats to text, as floats.
    Missing fields are simply absent — callers apply their own defaults."""
    out: dict[str, float] = {}
    pe = metrics.get("peBasicExclExtraTTM", metrics.get("peTTM"))
    if pe is not None:
        out["pe_ttm"] = float(pe)
    ev_fcf = metrics.get("currentEv/freeCashFlowTTM")
    if ev_fcf is not None:
        out["ev_fcf_ttm"] = float(ev_fcf)
    gross_margin = metrics.get("grossMarginTTM")
    if gross_margin is not None:
        out["gross_margin_ttm"] = float(gross_margin)
    rev_growth = metrics.get("revenueGrowthTTMYoy")
    if rev_growth is not None:
        out["revenue_growth_yoy"] = float(rev_growth)
    if recs:
        latest = recs[0]
        out["analyst_buy_count"] = float(latest.get("buy", 0))
        out["analyst_hold_count"] = float(latest.get("hold", 0))
        out["analyst_sell_count"] = float(latest.get("sell", 0))
    return out


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
    finnhub_client=None,  # optional, used instead of yfinance when set
    repo=None,  # optional PortfolioRepository — enables LLM cost logging
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
            _log_llm_call(repo, "cache", symbol, cache_hit=True, usage=None)
            return cached["action"] == "approve"

        # Try Finnhub first, fall back to yfinance
        if finnhub_client is not None:
            financials = fetch_fundamentals_finnhub(symbol, finnhub_client)
        else:
            financials = ""
        if not financials:
            financials = fetch_financials(symbol)  # existing yfinance fallback
        if not financials:
            logger.debug("fundamental gate no financials for %s — approving", symbol)
            return True  # not cached — retry fetch next tick

        price_ctx = _price_context(symbol, bars)
        user_message = f"{price_ctx}\n\nFinancial statements:\n{financials}"

        logger.info("fundamental gate request symbol=%s date=%s", symbol, date_str)

        raw, usage = call_llm(_SYSTEM_PROMPT, user_message, 128, groq_key, groq_model, claude_key, claude_model, gemini_key, gemini_model)
        if not raw:
            logger.warning("fundamental gate: no LLM response for %s, approving", symbol)
            return True
        _log_llm_call(repo, usage.provider if usage else "unknown", symbol, cache_hit=False, usage=usage)

        parsed = _parse_response(raw)
        action = parsed["action"]
        rationale = parsed.get("rationale", "")

        if action not in {"approve", "veto"}:
            raise ValueError(f"unexpected action {action!r}")

        # Prune prior-day entries — date-keyed cache would otherwise grow for process lifetime.
        for stale in [k for k in _FUNDAMENTAL_CACHE if k[1] != date_str]:
            del _FUNDAMENTAL_CACHE[stale]
        _FUNDAMENTAL_CACHE[cache_key] = {"action": action, "rationale": rationale}
        logger.info(
            "fundamental gate response symbol=%s action=%s rationale=%r",
            symbol, action, rationale,
        )
        return action == "approve"

    except Exception as exc:
        logger.warning("fundamental gate failed for %s, approving: %s", symbol, exc)
        return True
