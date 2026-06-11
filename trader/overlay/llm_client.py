"""Thin LLM dispatch: Gemini primary, Groq secondary, Claude last-resort. Non-load-bearing."""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0  # seconds per provider call
_RATE_LIMIT_RETRY_DELAY = 1.0  # seconds to wait before one retry on rate-limit


def call_llm(
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    groq_key: str | None,
    groq_model: str,
    claude_key: str | None,
    claude_model: str,
    gemini_key: str | None = None,
    gemini_model: str = "gemini-2.0-flash-lite",
) -> str:
    """Try Gemini → Groq → Claude; return '' if all absent or fail."""
    providers = []
    if groq_key:
        providers.append(("groq", lambda: _groq(system_prompt, user_message, max_tokens, groq_key, groq_model)))
    if gemini_key:
        providers.append(("gemini", lambda: _gemini(system_prompt, user_message, max_tokens, gemini_key, gemini_model)))
    if claude_key:
        providers.append(("claude", lambda: _claude(system_prompt, user_message, max_tokens, claude_key, claude_model)))

    for name, fn in providers:
        result = _call_with_retry(name, fn)
        if result is not None:
            return result

    return ""


def _call_with_retry(provider: str, fn) -> str | None:
    """Call fn(); on rate-limit error retry once after a short delay. Returns None on failure."""
    for attempt in range(2):
        if attempt > 0:
            time.sleep(_RATE_LIMIT_RETRY_DELAY)
        try:
            result = fn()
            logger.debug("llm via %s", provider)
            return result
        except Exception as exc:
            if _is_rate_limit(exc) and attempt == 0:
                logger.warning("%s rate-limit, retrying in %.0fs", provider, _RATE_LIMIT_RETRY_DELAY)
                continue
            if _is_rate_limit(exc):
                logger.warning("%s rate-limit exhausted, trying next provider", provider)
            else:
                logger.warning("%s failed (%s: %s), trying next provider", provider, type(exc).__name__, exc)
            return None
    return None


def _is_rate_limit(exc: Exception) -> bool:
    name = type(exc).__name__
    if "RateLimit" in name or "ResourceExhausted" in name or "TooManyRequests" in name:
        return True
    return getattr(exc, "status_code", None) == 429 or getattr(exc, "code", None) == 429


def _gemini(system_prompt: str, user_message: str, max_tokens: int, key: str, model: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=model,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=max_tokens,
        ),
    )
    return resp.text or ""


def _groq(system_prompt: str, user_message: str, max_tokens: int, key: str, model: str) -> str:
    from groq import Groq

    client = Groq(api_key=key, timeout=_TIMEOUT)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )
    return resp.choices[0].message.content or ""


def _claude(system_prompt: str, user_message: str, max_tokens: int, key: str, model: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=key, timeout=_TIMEOUT)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text or ""
