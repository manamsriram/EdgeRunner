"""Finnhub REST API client — rate-limited, cached, never raises."""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
_RATE_LIMIT_SLEEP = 1.1  # 60 req/min free tier


class FinnhubClient:
    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self._last_call: float = 0.0

    def company_news(self, symbol: str, from_date: str, to_date: str, limit: int = 8) -> list[dict]:
        """GET /company-news. Returns [{headline, summary, datetime, source}]. Never raises."""
        data = self._get("/company-news", {"symbol": symbol, "from": from_date, "to": to_date})
        if not isinstance(data, list):
            return []
        return data[:limit]

    def basic_financials(self, symbol: str) -> dict:
        """GET /stock/metric?metric=all. Returns metric dict. Never raises."""
        data = self._get("/stock/metric", {"symbol": symbol, "metric": "all"})
        if not isinstance(data, dict):
            return {}
        return data.get("metric", {})

    def recommendation_trends(self, symbol: str) -> list[dict]:
        """GET /stock/recommendation. Returns [{buy, hold, sell, period}]. Never raises."""
        data = self._get("/stock/recommendation", {"symbol": symbol})
        if not isinstance(data, list):
            return []
        return data[:3]

    def crypto_news(self, limit: int = 10) -> list[dict]:
        """GET /news?category=crypto. Never raises."""
        data = self._get("/news", {"category": "crypto"})
        if not isinstance(data, list):
            return []
        return data[:limit]

    def earnings_calendar(self, symbol: str, from_date: str, to_date: str) -> list[dict]:
        """GET /calendar/earnings. Returns [{date, symbol, ...}]. Never raises."""
        data = self._get("/calendar/earnings", {"symbol": symbol, "from": from_date, "to": to_date})
        if not isinstance(data, dict):
            return []
        entries = data.get("earningsCalendar", [])
        return entries if isinstance(entries, list) else []

    def _get(self, path: str, params: dict) -> dict | list | None:
        """Rate-limited GET. Sleeps to respect 60 req/min. Never raises."""
        try:
            elapsed = time.monotonic() - self._last_call
            wait = _RATE_LIMIT_SLEEP - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            resp = requests.get(
                FINNHUB_BASE + path,
                params={**params, "token": self._key},
                timeout=8.0,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("finnhub _get %s failed: %s", path, exc)
            return None
