"""Fire-and-forget alert delivery.

The trading path must never fail because an alert failed. Every public function
here swallows exceptions and logs a warning instead of propagating.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


def send_alert(message: str, webhook_url: str | None) -> None:
    """POST a Slack-compatible webhook alert.

    No-ops silently when `webhook_url` is None (e.g. unconfigured in .env).
    All network errors are caught and logged as warnings — never raised.
    """
    if not webhook_url:
        return
    try:
        resp = requests.post(webhook_url, json={"text": message}, timeout=5)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert delivery failed: %s", exc)
