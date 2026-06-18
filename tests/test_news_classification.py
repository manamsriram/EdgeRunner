"""Tests for news classification in news_context.py."""
from __future__ import annotations

from trader.overlay.news_context import classify_news, format_classified_news


def test_earnings_headline_classified():
    categories = classify_news(["Apple beats earnings estimates by 10%"])
    assert "EARNINGS" in categories


def test_regulatory_headline_classified():
    categories = classify_news(["SEC launches investigation into trading practices"])
    assert "REGULATORY" in categories


def test_ma_headline_classified():
    categories = classify_news(["Microsoft acquires Activision for $69 billion"])
    assert "M&A" in categories


def test_analyst_headline_classified():
    categories = classify_news(["Goldman Sachs upgrades AAPL to buy with $200 price target"])
    assert "ANALYST" in categories


def test_product_headline_classified():
    categories = classify_news(["Apple launches new iPhone 17 with AI features"])
    assert "PRODUCT" in categories


def test_neutral_headline_unclassified():
    categories = classify_news(["Stock market closes mixed on Wednesday"])
    assert categories == {}


def test_headline_can_match_multiple_categories():
    categories = classify_news(["SEC investigation triggers analyst downgrade"])
    assert "REGULATORY" in categories
    assert "ANALYST" in categories


def test_format_classified_news_returns_string():
    categories = {"EARNINGS": ["Company beats Q3 estimates"]}
    result = format_classified_news("AAPL", categories)
    assert "AAPL" in result
    assert "EARNINGS" in result
    assert "beats Q3" in result


def test_format_classified_news_empty_returns_empty():
    result = format_classified_news("AAPL", {})
    assert result == ""


def test_fetch_news_with_fallback_uses_finnhub_when_key_set():
    from unittest.mock import MagicMock, patch
    from trader.overlay.news_context import fetch_news_with_fallback

    mock_config = MagicMock()
    mock_config.finnhub_api_key = "test-key"

    with patch("trader.overlay.news_context.fetch_news_finnhub", return_value="[EARNINGS] Apple beats estimates") as mock_finnhub:
        result = fetch_news_with_fallback("AAPL", mock_config)

    mock_finnhub.assert_called_once_with("AAPL", "test-key")
    assert result == "[EARNINGS] Apple beats estimates"


def test_fetch_news_with_fallback_falls_back_when_no_key():
    from unittest.mock import MagicMock, patch
    from trader.overlay.news_context import fetch_news_with_fallback

    mock_config = MagicMock()
    mock_config.finnhub_api_key = None

    with patch("trader.overlay.news_context.fetch_news", return_value="Recent news: headline") as mock_fetch:
        result = fetch_news_with_fallback("AAPL", mock_config)

    mock_fetch.assert_called_once_with("AAPL")
    assert result == "Recent news: headline"


def test_fetch_news_with_fallback_falls_back_when_finnhub_empty():
    from unittest.mock import MagicMock, patch
    from trader.overlay.news_context import fetch_news_with_fallback

    mock_config = MagicMock()
    mock_config.finnhub_api_key = "test-key"

    with patch("trader.overlay.news_context.fetch_news_finnhub", return_value=""):
        with patch("trader.overlay.news_context.fetch_news", return_value="Alpaca headline") as mock_fetch:
            result = fetch_news_with_fallback("AAPL", mock_config)

    assert result == "Alpaca headline"
