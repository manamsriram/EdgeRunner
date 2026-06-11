"""Lightweight market context fetchers for the Claude overlay.

All functions return empty string on any failure (never raise).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import yfinance as yf


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
        articles = getattr(response, "news", None) or []
        headlines = [a.headline for a in articles[:4] if getattr(a, "headline", None)]
        if not headlines:
            return ""
        lines = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines))
        return f"Recent news ({symbol}):\n{lines}"

    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_fetch)
            return future.result(timeout=timeout + 1)
    except Exception:
        return ""


def fetch_financials(symbol: str, timeout: float = 5.0) -> str:
    """Fetch key financials (balance sheet + income statement) for a US ticker.

    Returns empty string on any failure — never raises.
    """
    import pandas as pd

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
