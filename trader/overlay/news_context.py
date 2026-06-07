"""Lightweight market context fetchers for the Claude overlay.

Salvaged from tools/fetch_stock_info.py — pure data-fetching, no LLM dependency.
All functions return empty string on any failure (never raise).
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import requests
import yfinance as yf
from bs4 import BeautifulSoup


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/101.0.4951.54 Safari/537.36"
    )
}

_CRYPTO_NAMES: dict[str, str] = {
    "BTC/USD": "Bitcoin",
    "ETH/USD": "Ethereum",
    "SOL/USD": "Solana",
    "XRP/USD": "XRP Ripple",
    "DOGE/USD": "Dogecoin",
    "ADA/USD": "Cardano",
    "AVAX/USD": "Avalanche crypto",
    "LINK/USD": "Chainlink crypto",
    "DOT/USD": "Polkadot crypto",
    "MATIC/USD": "Polygon crypto",
}


def _news_query(symbol: str) -> str:
    """Return a search query appropriate for equities or crypto symbols."""
    if "/" in symbol:
        name = _CRYPTO_NAMES.get(symbol.upper(), symbol.split("/")[0])
        return f"{name} crypto news"
    return f"{symbol} stock news"


def _google_news_url(symbol: str) -> str:
    query = _news_query(symbol)
    url = f"https://www.google.com/search?q={query}&gl=us&tbm=nws&num=5"
    return re.sub(r"\s", "+", url)


def fetch_news(symbol: str, timeout: float = 5.0) -> str:
    """Fetch recent news headlines for a US ticker symbol.

    Salvaged from tools/fetch_stock_info.get_recent_stock_news().
    Returns empty string on any failure — never raises.
    """
    def _fetch() -> str:
        url = _google_news_url(symbol)
        response = requests.get(url, headers=_HEADERS, timeout=timeout)
        soup = BeautifulSoup(response.content, "html.parser")
        articles = soup.select("div.SoaBEf")
        if not articles:
            return ""
        headlines = []
        for article in articles[:4]:
            title = article.select_one("div.MBeuO")
            if title:
                headlines.append(title.get_text())
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

    Salvaged from tools/fetch_stock_info.get_financial_statements().
    Fixes: removed time.sleep(4), US-ticker default, pd.concat replacing .append().
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
