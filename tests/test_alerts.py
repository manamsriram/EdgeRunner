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
