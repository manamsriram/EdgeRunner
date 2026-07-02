"""Lightweight market context fetchers for the Claude overlay.

All functions return empty string on any failure (never raise).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

# ---- News classification (FinRobot-inspired) ----

_NEWS_CATEGORIES: dict[str, list[str]] = {
    "EARNINGS": ["earnings", "revenue", "profit", "eps", "guidance", "beat", "miss", "quarter", "sales"],
    "REGULATORY": ["sec", "fda", "ftc", "antitrust", "fine", "penalty", "investigation", "ban", "lawsuit"],
    "M&A": ["acquisition", "merger", "takeover", "buyout", "deal", "acquires", "acquired"],
    "ANALYST": ["upgrade", "downgrade", "price target", "overweight", "underweight", "outperform", "buy rating"],
    "PRODUCT": ["launch", "release", "product", "partnership", "contract", "wins", "awarded"],
}


def classify_news(headlines: list[str]) -> dict[str, list[str]]:
    """Map each headline to matching categories. Returns {CATEGORY: [headlines]}."""
    result: dict[str, list[str]] = {}
    for headline in headlines:
        hl_lower = headline.lower()
        for category, keywords in _NEWS_CATEGORIES.items():
            if any(kw in hl_lower for kw in keywords):
                result.setdefault(category, []).append(headline)
    return result


def format_classified_news(symbol: str, categories: dict[str, list[str]]) -> str:
    """Format classified news for LLM user message. Returns '' if no categories."""
    if not categories:
        return ""
    parts = [f"Recent news ({symbol}):"]
    for cat, headlines in categories.items():
        for h in headlines[:2]:  # max 2 per category
            parts.append(f"[{cat}] {h}")
    return "\n".join(parts)


# ---- Finnhub-backed news fetch ----

import threading

# Module-level singleton (lazy-init, thread-safe)
_finnhub_client = None
_finnhub_lock = threading.Lock()


def _reset_finnhub_client() -> None:
    """Test helper — resets the Finnhub client singleton."""
    global _finnhub_client
    with _finnhub_lock:
        _finnhub_client = None


def _get_finnhub_client(api_key: str):
    global _finnhub_client
    if _finnhub_client is None:
        with _finnhub_lock:
            if _finnhub_client is None:
                from trader.data.finnhub_client import FinnhubClient
                _finnhub_client = FinnhubClient(api_key)
    return _finnhub_client


def fetch_news_finnhub(symbol: str, api_key: str) -> str:
    """Fetch and classify company news from Finnhub. Returns '' on any failure."""
    try:
        from datetime import date, timedelta
        client = _get_finnhub_client(api_key)
        today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        articles = client.company_news(symbol, from_date=week_ago, to_date=today, limit=8)
        headlines = [a.get("headline", "") for a in articles if a.get("headline")]
        if not headlines:
            return ""
        categories = classify_news(headlines)
        return format_classified_news(symbol, categories)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("fetch_news_finnhub failed for %s: %s", symbol, exc)
        return ""


def fetch_news_with_fallback(symbol: str, config) -> str:
    """Try Finnhub first (if FINNHUB_API_KEY set), fall back to fetch_news(). Never raises."""
    try:
        api_key = getattr(config, "finnhub_api_key", None)
        if api_key:
            result = fetch_news_finnhub(symbol, api_key)
            if result:
                return result
    except Exception:
        pass
    return fetch_news(symbol)


def fetch_news(
    symbol: str,
    timeout: float = 5.0,
    alpaca_api_key: str | None = None,
    alpaca_secret_key: str | None = None,
) -> str:
    """Fetch recent news headlines via Alpaca News API.

    Returns empty string on any failure — never raises.
    Keys are optional; if absent, loaded from config.
    """
    def _fetch() -> str:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest

        key = alpaca_api_key
        secret = alpaca_secret_key
        if not key or not secret:
            from trader.config import load_config
            cfg = load_config()
            key = cfg.alpaca_api_key
            secret = cfg.alpaca_secret_key

        client = NewsClient(api_key=key, secret_key=secret)
        query_symbol = symbol.split("/")[0] if "/" in symbol else symbol
        request = NewsRequest(symbols=query_symbol, limit=5)
        response = client.get_news(request)
        # NewsSet has no `.news` attribute — articles live at `.data["news"]`.
        articles = response.data.get("news", []) if hasattr(response, "data") else []
        headlines = [a.headline for a in articles[:4] if getattr(a, "headline", None)]
        if not headlines:
            return ""
        lines = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines))
        return f"Recent news ({symbol}):\n{lines}"

    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_fetch)
            return future.result(timeout=timeout + 1)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("fetch_news failed for %s: %s", symbol, exc)
        return ""


def fetch_financials(symbol: str, timeout: float = 5.0) -> str:
    """Fetch key financials (balance sheet + income statement) for a US ticker.

    Returns empty string on any failure — never raises.
    """
    import pandas as pd
    # Lazy import — keeps yfinance out of the scheduler process until actually needed.
    import yfinance as yf

    def _fetch() -> str:
        ticker = yf.Ticker(symbol)
        balance_sheet = ticker.balance_sheet
        if balance_sheet is None or balance_sheet.empty:
            return ""
        if balance_sheet.shape[1] >= 3:
            balance_sheet = balance_sheet.iloc[:, :3]
        balance_sheet = balance_sheet.dropna(how="any")
        try:
            income_stmt = ticker.income_stmt
            if income_stmt is not None and not income_stmt.empty:
                balance_sheet = pd.concat([balance_sheet, income_stmt.iloc[:5]])
        except Exception:
            pass
        return f"Financials ({symbol}):\n{balance_sheet.to_string()}"

    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_fetch)
            return future.result(timeout=timeout + 1)
    except Exception:
        return ""
