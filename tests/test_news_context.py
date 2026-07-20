"""Tests for news classification retaining datetime in news_context.py."""
import pytest

from trader.overlay import news_context
from trader.overlay.news_context import (
    _fetch_finnhub_articles_classified,
    classify_news,
    format_classified_news,
)


def test_classify_news_retains_datetime():
    articles = [
        {"headline": "Company beats earnings estimate", "datetime": "2026-07-10T09:00:00"},
        {"headline": "Random unrelated headline", "datetime": "2026-07-11T09:00:00"},
    ]
    result = classify_news(articles)
    assert "EARNINGS" in result
    assert result["EARNINGS"][0]["headline"] == "Company beats earnings estimate"
    assert result["EARNINGS"][0]["datetime"] == "2026-07-10T09:00:00"


def test_format_classified_news_unchanged_output_shape():
    articles = [{"headline": "Company beats earnings estimate", "datetime": "2026-07-10T09:00:00"}]
    categories = classify_news(articles)
    text = format_classified_news("AAPL", categories)
    assert "[EARNINGS] Company beats earnings estimate" in text
    assert "2026-07-10" not in text  # formatted text stays headline-only


# ---------------------------------------------------------------------------
# Shared Finnhub fetch cache — proves _fetch_finnhub_articles_classified is
# the SAME cache consulted by both fetch_news_finnhub (LLM overlay path) and
# trader/pipeline.py::_log_decision_features, not two disconnected caches.
# ---------------------------------------------------------------------------

class _FakeFinnhubClient:
    def __init__(self):
        self.call_count = 0

    def company_news(self, symbol, from_date, to_date, limit):
        self.call_count += 1
        return [{"headline": "Company beats earnings estimate", "datetime": "2026-07-10T09:00:00"}]


@pytest.fixture(autouse=True)
def _clear_articles_cache():
    news_context._reset_articles_cache()
    news_context._reset_finnhub_client()
    yield
    news_context._reset_articles_cache()
    news_context._reset_finnhub_client()


def test_second_caller_within_ttl_reuses_cached_articles(monkeypatch):
    """Two independent callers (simulating apply_claude_overlay and
    _log_decision_features) hitting the same symbol within the 60s TTL must
    result in exactly ONE underlying client.company_news call."""
    fake_client = _FakeFinnhubClient()
    monkeypatch.setattr(news_context, "_get_finnhub_client", lambda api_key: fake_client)

    first = _fetch_finnhub_articles_classified("AAPL", "fake-key")
    second = _fetch_finnhub_articles_classified("AAPL", "fake-key")

    assert fake_client.call_count == 1
    assert first == second
    assert "EARNINGS" in first


def test_different_symbol_is_a_cache_miss(monkeypatch):
    fake_client = _FakeFinnhubClient()
    monkeypatch.setattr(news_context, "_get_finnhub_client", lambda api_key: fake_client)

    _fetch_finnhub_articles_classified("AAPL", "fake-key")
    _fetch_finnhub_articles_classified("MSFT", "fake-key")

    assert fake_client.call_count == 2
