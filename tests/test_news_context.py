"""Tests for news classification retaining datetime in news_context.py."""
from trader.overlay.news_context import classify_news, format_classified_news


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
