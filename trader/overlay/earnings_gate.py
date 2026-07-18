"""Hard earnings-date gate — deterministic, no LLM.

Blocks new equity buy signals when an earnings release falls within the next
`days_ahead` trading days. Unlike the LLM overlay's soft earnings veto (which
depends on the news feed surfacing an earnings headline and defaults to
approve when uncertain), this checks the Finnhub earnings calendar directly.

Non-load-bearing: any failure (no client, API error, empty calendar) returns
True (approve) — same fail-open convention as fundamental_gate.py. Results
are cached in-process by (symbol, date_str) — at most one API call per
symbol per trading day.
"""
from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

# (symbol, date_str) -> bool (True = approved, no earnings in window)
_EARNINGS_CACHE: dict[tuple[str, str], bool] = {}


def _clear_cache() -> None:
    """Test helper — clears the in-process earnings cache."""
    _EARNINGS_CACHE.clear()


def check_earnings_gate(
    symbol: str,
    finnhub_client,
    date_str: str,
    days_ahead: int = 2,
) -> bool:
    """Return True (approved) unless an earnings release falls in the next
    `days_ahead` trading days for `symbol`. Never raises."""
    if finnhub_client is None or not date_str:
        return True

    cache_key = (symbol, date_str)
    if cache_key in _EARNINGS_CACHE:
        return _EARNINGS_CACHE[cache_key]

    try:
        today = pd.Timestamp(date_str)
        window_end = today + pd.tseries.offsets.BDay(days_ahead)
        entries = finnhub_client.earnings_calendar(
            symbol, today.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")
        )
        approved = not any(e.get("date") for e in entries if isinstance(e, dict))

        # Prune prior-day entries — date-keyed cache would otherwise grow for process lifetime.
        for stale in [k for k in _EARNINGS_CACHE if k[1] != date_str]:
            del _EARNINGS_CACHE[stale]
        _EARNINGS_CACHE[cache_key] = approved

        if not approved:
            logger.info("earnings gate veto symbol=%s date=%s window_end=%s", symbol, date_str, window_end.date())
        return approved
    except Exception as exc:
        logger.warning("earnings gate failed for %s, approving: %s", symbol, exc)
        return True
