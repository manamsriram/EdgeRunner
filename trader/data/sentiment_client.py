from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field

import logging

logger = logging.getLogger(__name__)

_BULL_WORDS = frozenset({"bull", "moon", "long", "buy", "pump", "breakout", "ath", "rally"})
_BEAR_WORDS = frozenset({"bear", "short", "sell", "crash", "dump", "rekt", "correction", "rug"})

_CACHE: dict[str, "SentimentSnapshot"] = {}
_CACHE_TTL = 4 * 3600.0
_EXECUTOR = ThreadPoolExecutor(max_workers=2)


@dataclass
class SentimentSnapshot:
    symbol: str
    bullish_ratio: float
    mention_count: int
    top_keywords: list[str]
    source: str
    fetched_at: float = field(default_factory=time.monotonic)


class SentimentClient:
    def __init__(
        self,
        finnhub_client=None,
        reddit_client_id: str | None = None,
        reddit_client_secret: str | None = None,
        reddit_user_agent: str = "EdgeRunner/1.0",
    ) -> None:
        self._finnhub = finnhub_client
        self._reddit_id = reddit_client_id
        self._reddit_secret = reddit_client_secret
        self._reddit_ua = reddit_user_agent

    def get_sentiment(self, symbol: str, timeout: float = 5.0) -> SentimentSnapshot | None:
        """Return cached snapshot or fetch fresh with hard timeout. Never raises, never blocks >timeout."""
        cached = _CACHE.get(symbol)
        if cached and (time.monotonic() - cached.fetched_at) < _CACHE_TTL:
            return cached
        try:
            future = _EXECUTOR.submit(self._fetch, symbol)
            snap = future.result(timeout=timeout)
            if snap:
                _CACHE[symbol] = snap
                return snap
            return cached  # fetch returned None → return stale if available
        except (FutureTimeout, Exception):
            return cached  # return stale cache on any failure

    def _fetch(self, symbol: str) -> SentimentSnapshot | None:
        """Try Reddit first, fall back to Finnhub crypto news. Never raises."""
        snap = None
        if self._reddit_id and self._reddit_secret:
            snap = self._fetch_reddit(symbol)
        if snap is None and self._finnhub is not None:
            snap = self._fetch_finnhub(symbol)
        return snap

    def _fetch_reddit(self, symbol: str) -> SentimentSnapshot | None:
        """Fetch from r/CryptoCurrency and r/Bitcoin via PRAW. Returns None if PRAW absent or error."""
        try:
            import praw
        except ImportError:
            return None
        try:
            reddit = praw.Reddit(
                client_id=self._reddit_id,
                client_secret=self._reddit_secret,
                user_agent=self._reddit_ua,
            )
            # Strip /USD or /USDT suffix; use base coin name
            coin = symbol.split("/")[0].lower()
            subreddit = reddit.subreddit("CryptoCurrency+Bitcoin")
            posts = list(subreddit.search(coin, limit=50, sort="new", time_filter="day"))
            return _score_posts(symbol, [p.title for p in posts], source="reddit")
        except Exception as exc:
            logger.debug("reddit sentiment fetch failed for %s: %s", symbol, exc)
            return None

    def _fetch_finnhub(self, symbol: str) -> SentimentSnapshot | None:
        """Fetch crypto headlines from Finnhub. Returns None on failure."""
        try:
            articles = self._finnhub.crypto_news(limit=20)
            coin = symbol.split("/")[0].lower()
            relevant = [a.get("headline", "") for a in articles
                        if coin in a.get("headline", "").lower() or
                           coin in a.get("summary", "").lower()]
            if not relevant:
                return None
            return _score_posts(symbol, relevant, source="finnhub")
        except Exception as exc:
            logger.debug("finnhub crypto news fetch failed for %s: %s", symbol, exc)
            return None

    def format_for_overlay(self, snap: SentimentSnapshot | None) -> str:
        """Format snapshot for LLM. Returns '' when snap is None."""
        if snap is None:
            return ""
        pct = int(snap.bullish_ratio * 100)
        kw = ", ".join(snap.top_keywords[:3]) if snap.top_keywords else "none"
        return (
            f"SENTIMENT ({snap.source}): {pct}% bullish "
            f"({snap.mention_count} mentions). Top keywords: {kw}."
        )


def _score_posts(symbol: str, texts: list[str], source: str) -> SentimentSnapshot | None:
    """Score a list of text snippets for bull/bear sentiment. Returns None if no texts."""
    if not texts:
        return None
    bull_count = 0
    bear_count = 0
    keyword_counts: dict[str, int] = {}
    for text in texts:
        words = set(text.lower().split())
        hits_bull = words & _BULL_WORDS
        hits_bear = words & _BEAR_WORDS
        if hits_bull:
            bull_count += 1
        if hits_bear:
            bear_count += 1
        for kw in (hits_bull | hits_bear):
            keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
    total = bull_count + bear_count
    bullish_ratio = bull_count / total if total > 0 else 0.5
    top_keywords = sorted(keyword_counts, key=lambda k: -keyword_counts[k])[:5]
    return SentimentSnapshot(
        symbol=symbol,
        bullish_ratio=bullish_ratio,
        mention_count=len(texts),
        top_keywords=top_keywords,
        source=source,
    )


def _reset_cache() -> None:
    """Test helper — clears the module-level sentiment cache."""
    _CACHE.clear()
