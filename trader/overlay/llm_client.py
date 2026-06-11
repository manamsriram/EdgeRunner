"""Thin LLM dispatch: Groq primary, Claude fallback. Non-load-bearing."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def call_llm(
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    groq_key: str | None,
    groq_model: str,
    claude_key: str | None,
    claude_model: str,
) -> str:
    """Try Groq first; fall back to Claude; return '' if both absent or fail."""
    if groq_key:
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            resp = client.chat.completions.create(
                model=groq_model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("groq call failed, falling back to claude: %s", exc)

    if claude_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=claude_key)
            resp = client.messages.create(
                model=claude_model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_message}],
            )
            return resp.content[0].text or ""
        except Exception as exc:
            logger.warning("claude call failed: %s", exc)

    return ""
