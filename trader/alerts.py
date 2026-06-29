"""Fire-and-forget alert delivery.

The trading path must never fail because an alert failed. Every public function
here swallows exceptions and logs a warning instead of propagating.

Email delivery uses the SendGrid HTTP API (port 443) rather than SMTP, because
cloud platforms (Render, Railway, etc.) commonly block outbound port 587/465.
Set SENDGRID_API_KEY + ALERT_EMAIL env vars to enable email alerts.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


def send_alert(
    message: str,
    webhook_url: str | None,
    *,
    alert_email: str | None = None,
    smtp_user: str | None = None,  # kept for backward-compat; used as sender address
    smtp_password: str | None = None,  # kept for backward-compat; treated as SendGrid key
) -> None:
    """Fire Slack webhook and/or email alert; swallows all errors.

    Email parameters (backward-compatible mapping):
      smtp_user      → sender address (your verified SendGrid sender)
      smtp_password  → SendGrid API key  (starts with 'SG.')
      alert_email    → recipient address
    """
    if webhook_url:
        try:
            resp = requests.post(webhook_url, json={"text": message}, timeout=5)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack alert failed: %s", exc)

    if alert_email and smtp_user and smtp_password:
        try:
            payload = {
                "personalizations": [{"to": [{"email": alert_email}]}],
                "from": {"email": smtp_user},
                "subject": f"EdgeRunner: {message[:80]}",
                "content": [{"type": "text/plain", "value": message}],
            }
            headers = {
                "Authorization": f"Bearer {smtp_password}",
                "Content-Type": "application/json",
            }
            resp = requests.post(
                _SENDGRID_URL, json=payload, headers=headers, timeout=10
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("email alert failed: %s", exc)
