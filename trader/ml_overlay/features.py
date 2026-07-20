"""Pure feature-vector builder for the ML-overlay research track.

Consumes objects the LLM overlay/gate already fetched this tick (bars, news,
sentiment, fundamentals, recent outcomes) — never re-fetches anything itself.
`bars` must already be asof-truncated (Strategy.generate's guarantee).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from trader.overlay.market_stats import compute_bar_stats
from trader.strategy.base import Signal

_NEWS_CATEGORY_KEYS = ("EARNINGS", "REGULATORY", "M&A", "ANALYST", "PRODUCT")
_REGIMES = ("calm", "normal", "stressed")

_FUNDAMENTAL_DEFAULTS = {
    "pe_ttm": 0.0,
    "ev_fcf_ttm": 0.0,
    "gross_margin_ttm": 0.0,
    "revenue_growth_yoy": 0.0,
    "analyst_buy_count": 0.0,
    "analyst_hold_count": 0.0,
    "analyst_sell_count": 0.0,
}


def build_feature_vector(
    signal: Signal,
    bars: pd.DataFrame,
    *,
    news_categories: dict[str, list[dict]],
    sentiment,  # SentimentSnapshot | None
    fundamentals: dict[str, float],
    recent_outcomes: list[dict],
    regime: str,
) -> dict[str, float]:
    """Build the numeric feature vector for one overlay decision.

    Deterministic given fixed inputs — no I/O, no clock reads except relative
    day-count math against `recent_outcomes`' own timestamps.
    """
    features: dict[str, float] = {"signal_strength": float(signal.strength)}

    bar_stats = compute_bar_stats(bars)
    features["last_close"] = bar_stats.get("last_close", 0.0)
    features["pct_20d"] = bar_stats.get("pct_20d", 0.0)
    features["vol_10d_annualized"] = bar_stats.get("vol_10d_annualized", 0.0)

    for cat in _NEWS_CATEGORY_KEYS:
        key = f"news_{cat.lower().replace('&', 'and')}_count"
        features[key] = float(len(news_categories.get(cat, [])))

    features["sentiment_bullish_ratio"] = float(sentiment.bullish_ratio) if sentiment else 0.0
    features["sentiment_mention_count"] = float(sentiment.mention_count) if sentiment else 0.0

    for key, default in _FUNDAMENTAL_DEFAULTS.items():
        features[f"fund_{key}"] = float(fundamentals.get(key, default))

    if recent_outcomes:
        features["last_trade_pnl_pct"] = float(recent_outcomes[0]["pnl_pct"])
        wins = sum(1 for o in recent_outcomes[:3] if o["pnl_pct"] > 0)
        features["win_rate_last_3"] = wins / min(len(recent_outcomes), 3)
        last_closed = datetime.fromisoformat(recent_outcomes[0]["closed_at"])
        if last_closed.tzinfo is None:
            last_closed = last_closed.replace(tzinfo=timezone.utc)
        days_since = (datetime.now(timezone.utc) - last_closed).total_seconds() / 86400.0
        features["days_since_last_trade"] = max(days_since, 0.0)
    else:
        features["last_trade_pnl_pct"] = 0.0
        features["win_rate_last_3"] = 0.0
        features["days_since_last_trade"] = -1.0

    for r in _REGIMES:
        features[f"regime_{r}"] = 1.0 if regime == r else 0.0

    return features
