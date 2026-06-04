"""Claude LLM overlay — Phase 5.

Non-load-bearing: any failure returns the original signal unchanged.
Claude may veto (side→"hold", strength=0.0) or adjust strength ∈ [0.0, 1.0].
It never originates a trade, flips buy↔sell, or sets position size.
"""
from __future__ import annotations

import json
import logging
import os
import re

import pandas as pd

from trader.strategy.base import Signal

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a non-load-bearing review layer in a quantitative trading pipeline.
A quant strategy has generated a signal. Your job: review it with market context
and decide whether to approve or veto it. You may NEVER flip buy↔sell, originate
a trade, or set position size.

Respond ONLY with valid JSON, no markdown, no explanation outside the JSON:
{"action": "approve"|"veto", "strength": <float 0.0-1.0>, "rationale": "<one sentence>"}

Rules:
- "veto": you believe the signal is wrong given current context. Sets side to "hold".
- "approve": you agree (or are uncertain). Set strength within [0.0, 1.0].
- If uncertain, approve with the original strength unchanged.
- The risk gate makes the final call regardless of your output.
"""


def _bars_context(symbol: str, bars: pd.DataFrame) -> str:
    if bars.empty or len(bars) < 2:
        return f"Insufficient bar data for {symbol}."
    close = bars["close"]
    last_close = float(close.iloc[-1])
    lookback_20 = min(20, len(close) - 1)
    pct_20d = (close.iloc[-1] / close.iloc[-lookback_20] - 1) * 100
    lookback_10 = min(10, len(close) - 1)
    returns_10 = close.pct_change().dropna().iloc[-lookback_10:]
    vol_10d = float(returns_10.std() * (252 ** 0.5) * 100) if len(returns_10) > 1 else 0.0
    n_days = len(close)
    return (
        f"Market context ({symbol}, last {n_days} trading days):\n"
        f"- Last close: ${last_close:.2f}\n"
        f"- {lookback_20}-day price change: {pct_20d:+.1f}%\n"
        f"- {lookback_10}-day annualized volatility: {vol_10d:.1f}%"
    )


def _parse_response(text: str) -> dict:
    """Parse Claude's response, stripping markdown fences if present."""
    text = text.strip()
    # Strip ```json ... ``` or ``` ... ``` wrappers
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


def apply_claude_overlay(
    signal: Signal,
    bars: pd.DataFrame,
    api_key: str,
    model: str,
) -> Signal:
    """Call Claude to review a quant signal. Returns original signal on any failure."""
    try:
        if anthropic is None:
            raise RuntimeError("anthropic package not installed; add anthropic>=0.40.0 to requirements")

        client = anthropic.Anthropic(api_key=api_key)

        user_message = (
            f"Signal: {signal.side} {signal.symbol} | "
            f"strength={signal.strength:.2f} | reason: {signal.reason}\n\n"
            f"{_bars_context(signal.symbol, bars)}"
        )

        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text
        parsed = _parse_response(raw)

        action = parsed.get("action")
        strength = parsed.get("strength")
        rationale = str(parsed.get("rationale", ""))

        if action not in {"approve", "veto"}:
            raise ValueError(f"invalid action: {action!r}")
        if not isinstance(strength, (int, float)) or not (0.0 <= float(strength) <= 1.0):
            raise ValueError(f"strength out of range: {strength!r}")

        if action == "veto":
            return Signal(
                symbol=signal.symbol,
                side="hold",
                strength=0.0,
                reason=f"[overlay veto] {rationale}",
            )

        return Signal(
            symbol=signal.symbol,
            side=signal.side,
            strength=float(strength),
            reason=f"[overlay approved] {rationale}",
        )

    except Exception as exc:  # noqa: BLE001
        logger.warning("claude overlay failed for %s, passing through: %s", signal.symbol, exc)
        return signal
