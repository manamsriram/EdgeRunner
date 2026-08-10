"""Alert module tests: fire-and-forget contract, no real HTTP."""
from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import requests

from trader.alerts import send_alert
from trader.config import Config, RiskLimits
from trader.pipeline import run_daily_trade_digest
from trader.portfolio.repository import OrderRow
from trader.portfolio.sqlite_repo import SQLiteRepository


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


# --- SMTP email path ---

def test_send_alert_email_calls_smtp():
    """Email path must log in and send via smtplib.SMTP with correct fields."""
    with patch("trader.alerts.smtplib.SMTP") as mock_smtp_cls:
        mock_smtp = mock_smtp_cls.return_value.__enter__.return_value
        send_alert(
            "fill: bought AAPL",
            None,
            alert_email="dest@example.com",
            smtp_user="sender@example.com",
            smtp_password="app-password",
        )
        mock_smtp_cls.assert_called_once_with("smtp.gmail.com", 587)
        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once_with("sender@example.com", "app-password")
        mock_smtp.send_message.assert_called_once()
        sent_msg = mock_smtp.send_message.call_args[0][0]
        assert sent_msg["From"] == "sender@example.com"
        assert sent_msg["To"] == "dest@example.com"
        assert "fill: bought AAPL" in sent_msg["Subject"]


def test_send_alert_email_missing_credentials_skips():
    """Email block must be skipped when credentials are absent."""
    with patch("trader.alerts.smtplib.SMTP") as mock_smtp_cls:
        # No webhook, no credentials — nothing should fire.
        send_alert("msg", None, alert_email="dest@example.com")
        mock_smtp_cls.assert_not_called()


def test_send_alert_email_swallows_smtp_error():
    """SMTP errors must not propagate to the caller."""
    with patch(
        "trader.alerts.smtplib.SMTP",
        side_effect=smtplib.SMTPException("auth failed"),
    ):
        # Must not raise.
        send_alert(
            "msg",
            None,
            alert_email="dest@example.com",
            smtp_user="sender@example.com",
            smtp_password="bad-password",
        )


# --- Daily trade digest (aggregation replacing per-fill emails) ---

def _config() -> Config:
    return Config(
        alpaca_api_key="k", alpaca_secret_key="s", alpaca_paper=True,
        autonomy="manual", openai_api_key=None, anthropic_api_key=None,
        portfolio_db_path=":memory:", kill_switch_path="ks.flag",
        risk=RiskLimits(allowlist=("AAPL",)),
        alert_email="dest@example.com", smtp_user="sender@example.com",
        smtp_password="app-password",
    )


def test_daily_digest_sends_one_email_for_all_fills(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "portfolio.db"))
    repo.record_order(OrderRow(
        client_order_id="c1", symbol="AAPL", side="buy",
        notional=1000.0, status="filled", fill_price=150.0,
    ))
    repo.record_order(OrderRow(
        client_order_id="c2", symbol="BTC/USD", side="buy",
        notional=500.0, status="filled", fill_price=60000.0,
    ))
    since = datetime.now(timezone.utc) - timedelta(days=1)
    with patch("trader.alerts.smtplib.SMTP") as mock_smtp_cls:
        mock_smtp = mock_smtp_cls.return_value.__enter__.return_value
        n = run_daily_trade_digest(_config(), repo, since, datetime.now(timezone.utc))
        assert n == 2
        mock_smtp.send_message.assert_called_once()
        sent_msg = mock_smtp.send_message.call_args[0][0]
        body = sent_msg.get_content()
        assert "AAPL" in body and "BTC/USD" in body


def test_daily_digest_skips_email_when_no_fills(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "portfolio.db"))
    since = datetime.now(timezone.utc) - timedelta(days=1)
    with patch("trader.alerts.smtplib.SMTP") as mock_smtp_cls:
        n = run_daily_trade_digest(_config(), repo, since, datetime.now(timezone.utc))
        assert n == 0
        mock_smtp_cls.assert_not_called()
