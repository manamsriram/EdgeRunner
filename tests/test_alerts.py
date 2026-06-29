"""Alert module tests: fire-and-forget contract, no real HTTP."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from trader.alerts import send_alert


def test_send_alert_posts_json():
    with patch("trader.alerts.requests.post") as mock_post:
        send_alert("hello world", "https://hooks.example.com/abc")
        mock_post.assert_called_once_with(
            "https://hooks.example.com/abc",
            json={"text": "hello world"},
            timeout=5,
        )


def test_send_alert_no_webhook_skips_request():
    with patch("trader.alerts.requests.post") as mock_post:
        send_alert("hello", None)
        mock_post.assert_not_called()


def test_send_alert_swallows_connection_error():
    with patch("trader.alerts.requests.post", side_effect=requests.ConnectionError("down")):
        # Must not raise.
        send_alert("msg", "https://hooks.example.com/abc")


def test_send_alert_swallows_timeout():
    with patch("trader.alerts.requests.post", side_effect=requests.Timeout("timed out")):
        send_alert("msg", "https://hooks.example.com/abc")


# --- SendGrid email path ---

def test_send_alert_email_calls_sendgrid():
    """Email path must POST to SendGrid API with correct headers and payload."""
    with patch("trader.alerts.requests.post") as mock_post:
        mock_post.return_value.raise_for_status = MagicMock()
        send_alert(
            "fill: bought AAPL",
            None,
            alert_email="dest@example.com",
            smtp_user="sender@example.com",
            smtp_password="SG.fakekey",
        )
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer SG.fakekey"
        payload = call_kwargs.kwargs["json"]
        assert payload["from"]["email"] == "sender@example.com"
        assert payload["personalizations"][0]["to"][0]["email"] == "dest@example.com"
        assert "fill: bought AAPL" in payload["subject"]


def test_send_alert_email_missing_credentials_skips():
    """Email block must be skipped when credentials are absent."""
    with patch("trader.alerts.requests.post") as mock_post:
        # No webhook, no credentials — nothing should fire.
        send_alert("msg", None, alert_email="dest@example.com")
        mock_post.assert_not_called()


def test_send_alert_email_swallows_sendgrid_error():
    """SendGrid HTTP errors must not propagate to the caller."""
    with patch(
        "trader.alerts.requests.post",
        side_effect=requests.HTTPError("403 Forbidden"),
    ):
        # Must not raise.
        send_alert(
            "msg",
            None,
            alert_email="dest@example.com",
            smtp_user="sender@example.com",
            smtp_password="SG.badkey",
        )
