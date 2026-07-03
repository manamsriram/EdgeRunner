"""Fire-and-forget alert delivery.

The trading path must never fail because an alert failed. Every public function
here swallows exceptions and logs a warning instead of propagating.

Set SMTP_USER + SMTP_PASSWORD + ALERT_EMAIL env vars to enable email alerts.
SMTP_USER is your sending address (e.g. Gmail), SMTP_PASSWORD is an app
password, not your account password.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import requests

logger = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


def send_alert(
    message: str,
    webhook_url: str | None,
    *,
    alert_email: str | None = None,
    smtp_user: str | None = None,
    smtp_password: str | None = None,
) -> None:
    """Fire Slack webhook and/or email alert; swallows all errors."""
    if webhook_url:
        try:
            resp = requests.post(webhook_url, json={"text": message}, timeout=5)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack alert failed: %s", exc)

    if alert_email and smtp_user and smtp_password:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"EdgeRunner: {message[:80]}"
            msg["From"] = smtp_user
            msg["To"] = alert_email
            msg.set_content(message)
            with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as smtp:
                smtp.starttls()
                smtp.login(smtp_user, smtp_password)
                smtp.send_message(msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("email alert failed: %s", exc)
