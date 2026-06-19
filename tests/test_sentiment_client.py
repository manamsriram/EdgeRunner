"""Tests for trader/data/sentiment_client.py — Integration 4: Crypto Sentiment Layer.

All tests run offline (no real API calls). Reddit/Finnhub are mocked.
"""
import time
import pytest
from trader.data.sentiment_client import (
    SentimentClient,
    SentimentSnapshot,
    _reset_cache,
    _score_posts,
)


@pytest.fixture(autouse=True)
def clear_cache():
    _reset_cache()
    yield
    _reset_cache()


def test_score_posts_bullish():
    texts = ["BTC moon rally buy", "pump incoming", "breakout confirmed ath"]
    snap = _score_posts("BTC/USD", texts, source="test")
    assert snap is not None
    assert snap.bullish_ratio > 0.5
    assert snap.mention_count == 3


def test_score_posts_bearish():
    texts = ["crash incoming sell", "bear market rekt", "correction dump"]
    snap = _score_posts("BTC/USD", texts, source="test")
    assert snap is not None
    assert snap.bullish_ratio <= 0.5


def test_score_posts_empty_returns_none():
    assert _score_posts("BTC/USD", [], source="test") is None


def test_score_posts_neutral_returns_half():
    # No bull or bear words → 0 total → default 0.5
    snap = _score_posts("BTC/USD", ["hello world today news"], source="test")
    assert snap is not None
    assert snap.bullish_ratio == pytest.approx(0.5)


def test_format_for_overlay_none_returns_empty():
    client = SentimentClient()
    assert client.format_for_overlay(None) == ""


def test_format_for_overlay_formats_correctly():
    snap = SentimentSnapshot(
        symbol="BTC/USD",
        bullish_ratio=0.68,
        mention_count=143,
        top_keywords=["moon", "rally", "ath"],
        source="reddit",
        fetched_at=time.monotonic(),
    )
    client = SentimentClient()
    result = client.format_for_overlay(snap)
    assert "68%" in result
    assert "143" in result
    assert "moon" in result
    assert "reddit" in result


def test_get_sentiment_returns_none_without_credentials():
    """No Reddit creds, no Finnhub client → fetch returns None."""
    client = SentimentClient()
    result = client.get_sentiment("BTC/USD", timeout=2.0)
    assert result is None


def test_get_sentiment_returns_cached_on_timeout():
    """Pre-warm cache; even if _fetch times out, stale cache returned."""
    snap = SentimentSnapshot(
        symbol="ETH/USD",
        bullish_ratio=0.6,
        mention_count=10,
        top_keywords=["bull"],
        source="test",
        fetched_at=time.monotonic(),
    )
    from trader.data.sentiment_client import _CACHE
    _CACHE["ETH/USD"] = snap
    client = SentimentClient()
    result = client.get_sentiment("ETH/USD", timeout=2.0)
    assert result is snap  # cache hit, no fetch needed


def test_get_sentiment_cache_ttl_expired():
    """Expired cache (fetched_at in the past) is NOT returned on miss."""
    snap = SentimentSnapshot(
        symbol="SOL/USD",
        bullish_ratio=0.5,
        mention_count=5,
        top_keywords=[],
        source="test",
        fetched_at=time.monotonic() - (4 * 3600 + 1),  # expired
    )
    from trader.data.sentiment_client import _CACHE
    _CACHE["SOL/USD"] = snap
    client = SentimentClient()  # no credentials
    result = client.get_sentiment("SOL/USD", timeout=2.0)
    # No creds → fetch returns None → stale fallback returned
    assert result is snap  # stale returned on failure path


def test_finnhub_fallback_uses_crypto_news():
    """Mock Finnhub client with crypto_news returning relevant headlines."""
    class FakeFinnhub:
        def crypto_news(self, limit=10):
            return [
                {"headline": "BTC moon rally incoming pump"},
                {"headline": "Bitcoin breakout ath"},
            ]

    client = SentimentClient(finnhub_client=FakeFinnhub())
    snap = client.get_sentiment("BTC/USD", timeout=2.0)
    assert snap is not None
    assert snap.source == "finnhub"
    assert snap.bullish_ratio > 0.5


def test_sentiment_client_never_raises_on_exception():
    """Even if _fetch throws internally, get_sentiment returns None or stale."""
    class BrokenFinnhub:
        def crypto_news(self, **_):
            raise RuntimeError("boom")

    client = SentimentClient(finnhub_client=BrokenFinnhub())
    result = client.get_sentiment("BTC/USD", timeout=2.0)
    assert result is None  # no raise, no crash
