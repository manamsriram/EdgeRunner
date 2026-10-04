"""Jev (TypeSafe AI) overlay — typed-decision alternative to the chat-LLM overlay.

Same contract as claude_overlay: non-load-bearing, any failure returns the original
signal; may veto or shrink a buy, never raises strength, never flips sides, never
touches sells (a vetoed exit strands the position).

Jev is not a numeric model and cannot explain itself, so price context is digested to
words in code, the judgment is split into atomic questions, and the veto/shrink policy
lives here in code. Reasons use a `[jev ...]` prefix so build_ml_dataset's
`[overlay ...]` label query never mistakes a Jev decision for an LLM label.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import pandas as pd

from trader.overlay.llm_client import LLMUsage
from trader.overlay.market_stats import compute_bar_stats
from trader.overlay.news_context import fetch_news, fetch_news_with_fallback
from trader.strategy.base import Signal

logger = logging.getLogger(__name__)

# ---- Config ----

JEV_PROVIDER = "jev"
DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_VETO_NOUL = 0.8
DEFAULT_TIMEOUT_S = 3.0
MAX_RETRIES = 1
MIN_SCORE_CONFIDENCE = 0.5  # below this the risk score is too diffuse to act on
MAX_NEWS_CHARS = 6000
_OVERLAY_TTL = 1800.0
# event_risk level -> strength multiplier
_RISK_MULTIPLIER = (1.0, 0.75, 0.5, 0.25)

# Circuit breaker so an outage can't stall the serial scheduler tick (fail-open stays).
BREAKER_FAILURES = 5
BREAKER_COOLDOWN_S = 300.0

# ---- Client + breaker ----

_client = None
_client_lock = threading.Lock()
_breaker_lock = threading.Lock()
_consecutive_failures = 0
_breaker_open_until = 0.0
_JEV_CACHE: dict[tuple[str, str], tuple[float, Signal]] = {}


def _reset_state() -> None:
    """Test helper."""
    global _client, _consecutive_failures, _breaker_open_until
    with _client_lock:
        _client = None
    with _breaker_lock:
        _consecutive_failures = 0
        _breaker_open_until = 0.0
    _JEV_CACHE.clear()


def _get_client(api_key: str):
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from typesafe_sdk import RetryPolicy, TypeSafeClient

                timeout = float(os.getenv("JEV_TIMEOUT_S", DEFAULT_TIMEOUT_S))
                _client = TypeSafeClient(
                    api_key=api_key,
                    retry=RetryPolicy(max_retries=MAX_RETRIES, timeout=timeout),
                )
    return _client


def _breaker_is_open() -> bool:
    with _breaker_lock:
        return time.monotonic() < _breaker_open_until


def _note_result(ok: bool) -> None:
    global _consecutive_failures, _breaker_open_until
    with _breaker_lock:
        if ok:
            _consecutive_failures = 0
            return
        _consecutive_failures += 1
        if _consecutive_failures >= BREAKER_FAILURES:
            _breaker_open_until = time.monotonic() + BREAKER_COOLDOWN_S
            _consecutive_failures = 0
            logger.error("jev overlay: %d consecutive failures, skipping for %.0fs",
                         BREAKER_FAILURES, BREAKER_COOLDOWN_S)


# ---- State + questions ----

def _price_words(bars: pd.DataFrame) -> str:
    stats = compute_bar_stats(bars)
    if not stats:
        return "insufficient price history"
    pct = stats["pct_20d"]
    vol = stats["vol_10d_annualized"]
    trend = "sharply extended" if pct > 25 else "rising" if pct > 5 else "falling" if pct < -5 else "flat"
    volatility = "high" if vol > 60 else "moderate" if vol > 30 else "low"
    return f"{stats['lookback_20']}-day trend {trend} ({pct:+.0f}%); volatility {volatility}"


def build_state(signal: Signal, bars: pd.DataFrame, news: str, sentiment: str) -> dict:
    return {
        "signal": {"side": signal.side, "symbol": signal.symbol, "reason": signal.reason},
        "price_context": _price_words(bars),
        # Headlines are third-party text: Jev can be steered by instructions in them, so
        # policy stays in code and Jev only emits probabilities.
        "news_headlines_untrusted": news[:MAX_NEWS_CHARS],
        "social_sentiment": sentiment,
    }


def _build_questions(is_crypto: bool):
    from typesafe_sdk import Noul, Score

    asset = "cryptocurrency" if is_crypto else "company"
    adverse = (
        "exchange hack, exploit, ban or seizure, de-pegging, insolvency"
        if is_crypto else
        "fraud or accounting problem, guidance cut, earnings miss, regulatory action, "
        "lawsuit, recall, trading halt"
    )
    return {
        "negative_catalyst": Noul(
            instructions=f"Do the headlines in `news_headlines_untrusted` report a material "
                         f"negative event for this specific {asset}?",
            criteria={
                "true": f"A concrete adverse event: {adverse}.",
                "false": "Routine, neutral, positive or unrelated news, or no adverse event.",
            },
        ),
        "thesis_contradicted": Noul(
            instructions="Does the news or sentiment contradict the direction of the trade in `signal`?",
            criteria={
                "true": "The news or sentiment argues against the trade `signal.side` on `signal.symbol`.",
                "false": "The news or sentiment is neutral, supportive, or unrelated to the trade.",
            },
        ),
        "event_risk": Score(
            instructions=f"How much event risk does the news add to a new position in this {asset}?",
            criteria=[
                "Routine or no relevant news",
                "A minor negative item with limited expected impact",
                "A material negative development likely to move the price",
                "A thesis-breaking event such as fraud, halt, hack or ban",
            ],
        ),
    }


# ---- Decision ----

def _judge(signal: Signal, state: dict, api_key: str, model: str):
    """Call Jev; return (veto, multiplier, answers_dict, usage) or None on failure."""
    if _breaker_is_open():
        return None
    try:
        resp = _get_client(api_key).system_one(
            state, _build_questions("/" in signal.symbol), model=model,
        )
    except Exception as exc:  # noqa: BLE001 — fail open, but loudly
        _note_result(False)
        logger.warning("jev overlay call failed for %s: %s", signal.symbol, exc)
        return None
    _note_result(True)

    veto_at = float(os.getenv("JEV_VETO_NOUL", DEFAULT_VETO_NOUL))
    neg = resp.answers["negative_catalyst"].noul
    contra = resp.answers["thesis_contradicted"].noul
    risk = resp.answers["event_risk"]
    multiplier = 1.0
    if risk.confidence >= MIN_SCORE_CONFIDENCE:
        level = min(max(round(risk.score), 0), len(_RISK_MULTIPLIER) - 1)
        multiplier = _RISK_MULTIPLIER[level]
    answers = {
        "negative_catalyst": round(neg, 3),
        "thesis_contradicted": round(contra, 3),
        "event_risk": round(risk.score, 3),
        "event_risk_conf": round(risk.confidence, 3),
        "multiplier": multiplier,
    }
    usage = LLMUsage(
        provider="typesafe", model=model,
        input_tokens=resp.usage.input_tokens or 0, output_tokens=resp.usage.output_tokens or 0,
    )
    return (neg >= veto_at or contra >= veto_at), multiplier, answers, usage


def _decide(signal: Signal, veto: bool, multiplier: float, answers: dict) -> Signal:
    detail = ", ".join(f"{k}={v}" for k, v in answers.items())
    if veto:
        return Signal(signal.symbol, "hold", 0.0, f"[jev veto] {detail}")
    strength = min(signal.strength, signal.strength * multiplier)  # never raise
    return Signal(signal.symbol, signal.side, strength, f"[jev approved] {detail}")


def _log(repo, run_id, signal: Signal, state: dict, result: Signal, answers: dict, usage) -> None:
    from trader.overlay.claude_overlay import _log_llm_call, _record_overlay_decision

    _log_llm_call(repo, "typesafe", signal.symbol, cache_hit=False, usage=usage)
    _record_overlay_decision(
        repo, run_id, signal.symbol, json.dumps(state, sort_keys=True),
        action="veto" if result.side == "hold" else "approve",
        strength_post=result.strength, rationale=json.dumps(answers, sort_keys=True),
        provider=JEV_PROVIDER,
    )


def _applies(signal: Signal, news: str, sentiment: str) -> bool:
    # Sells pass through; with nothing to read, Jev would only produce noise.
    return signal.side == "buy" and bool(news or sentiment)


# ---- Entry points ----

def shadow_jev(signal: Signal, bars: pd.DataFrame, news: str, sentiment: str,
               api_key: str, repo=None, run_id: int | None = None) -> None:
    """Run Jev beside the LLM and log its decision only. Never raises, never decides."""
    try:
        if not _applies(signal, news, sentiment):
            return
        state = build_state(signal, bars, news, sentiment)
        out = _judge(signal, state, api_key, os.getenv("JEV_MODEL", DEFAULT_JEV_MODEL))
        if out is None:
            return
        veto, multiplier, answers, usage = out
        result = _decide(signal, veto, multiplier, answers)
        logger.info("jev shadow symbol=%s would=%s %s", signal.symbol, result.side, answers)
        _log(repo, run_id, signal, state, result, answers, usage)
    except Exception:
        logger.warning("jev shadow failed for %s", signal.symbol, exc_info=True)


def apply_jev_overlay(signal: Signal, bars: pd.DataFrame, api_key: str, config=None,
                      sentiment_client=None, repo=None, run_id: int | None = None) -> Signal:
    """Jev decides. Returns the original signal on any failure."""
    try:
        cache_key = (signal.symbol, signal.side)
        if repo is not None:
            try:
                row = repo.get_overlay_cache(signal.symbol, signal.side, _OVERLAY_TTL)
            except Exception:
                logger.warning("jev cache read failed for %s", signal.symbol, exc_info=True)
                row = None
            if row is not None:
                return Signal(signal.symbol, row["side"], row["strength"], row["reason"])
        elif cache_key in _JEV_CACHE and time.monotonic() - _JEV_CACHE[cache_key][0] < _OVERLAY_TTL:
            return _JEV_CACHE[cache_key][1]

        if signal.side != "buy":
            return signal
        news = (fetch_news_with_fallback(signal.symbol, config) if config
                else fetch_news(signal.symbol)) or ""
        sentiment = ""
        if "/" in signal.symbol and sentiment_client is not None:
            sentiment = sentiment_client.format_for_overlay(sentiment_client.get_sentiment(signal.symbol))
        if not _applies(signal, news, sentiment):
            return signal

        state = build_state(signal, bars, news, sentiment)
        out = _judge(signal, state, api_key, os.getenv("JEV_MODEL", DEFAULT_JEV_MODEL))
        if out is None:
            return signal
        veto, multiplier, answers, usage = out
        result = _decide(signal, veto, multiplier, answers)
        _log(repo, run_id, signal, state, result, answers, usage)

        if repo is not None:
            try:
                repo.set_overlay_cache(signal.symbol, signal.side, result.side, result.strength, result.reason)
            except Exception:
                logger.warning("jev cache write failed for %s", signal.symbol, exc_info=True)
        else:
            _JEV_CACHE[cache_key] = (time.monotonic(), result)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev overlay failed for %s, passing through: %s", signal.symbol, exc)
        return signal
