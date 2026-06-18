"""Tests for FinnhubClient — uses unittest.mock.patch."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trader.data.finnhub_client import FinnhubClient


@pytest.fixture
def client():
    return FinnhubClient(api_key="test-key")


def test_company_news_returns_list_of_headlines(client):
    mock_data = [
        {"headline": "AAPL beats earnings", "summary": "...", "datetime": 1700000000, "source": "reuters"},
        {"headline": "Apple launches new product", "summary": "...", "datetime": 1700000001, "source": "bloomberg"},
    ]
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        result = client.company_news("AAPL", "2024-01-01", "2024-01-07")
    assert len(result) == 2
    assert result[0]["headline"] == "AAPL beats earnings"


def test_company_news_never_raises_on_error(client):
    with patch("requests.get", side_effect=ConnectionError("network down")):
        result = client.company_news("AAPL", "2024-01-01", "2024-01-07")
    assert result == []


def test_basic_financials_extracts_metric_dict(client):
    mock_data = {"metric": {"peBasicExclExtraTTM": 25.3, "grossMarginTTM": 42.5}, "metricType": "annual"}
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        result = client.basic_financials("AAPL")
    assert result.get("peBasicExclExtraTTM") == pytest.approx(25.3)
    assert result.get("grossMarginTTM") == pytest.approx(42.5)


def test_basic_financials_never_raises_on_error(client):
    with patch("requests.get", side_effect=TimeoutError()):
        result = client.basic_financials("AAPL")
    assert result == {}


def test_recommendation_trends_returns_list(client):
    mock_data = [{"buy": 20, "hold": 5, "sell": 2, "period": "2024-01-01"}]
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        result = client.recommendation_trends("AAPL")
    assert result[0]["buy"] == 20


def test_get_returns_none_on_http_error(client):
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("404 Not Found")
        mock_get.return_value = mock_resp
        result = client._get("/company-news", {"symbol": "AAPL"})
    assert result is None


def test_company_news_respects_limit(client):
    mock_data = [
        {"headline": f"Headline {i}", "datetime": 1700000000 + i}
        for i in range(10)
    ]
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        result = client.company_news("AAPL", "2024-01-01", "2024-01-07", limit=3)
    assert len(result) == 3


def test_basic_financials_empty_on_non_dict(client):
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        result = client.basic_financials("AAPL")
    assert result == {}


def test_crypto_news_returns_list(client):
    mock_data = [{"headline": "Bitcoin hits $100k", "datetime": 1700000000}]
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        result = client.crypto_news()
    assert len(result) == 1
    assert result[0]["headline"] == "Bitcoin hits $100k"


def test_crypto_news_never_raises_on_error(client):
    with patch("requests.get", side_effect=Exception("timeout")):
        result = client.crypto_news()
    assert result == []
