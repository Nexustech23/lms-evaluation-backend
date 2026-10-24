from app.core.diagnostics import trace_function
import logging
import re
from typing import Dict, Tuple

import anthropic

from app.core.config import settings

_client: anthropic.Anthropic | None = None


def _response_text(message):
    """Read text blocks without treating an empty/non-text response as text."""
    parts = []
    for block in getattr(message, "content", None) or []:
        block_type = getattr(block, "type", None)
        if isinstance(block_type, str) and block_type != "text":
            continue
        value = getattr(block, "text", None)
        if isinstance(value, str):
            parts.append(value)
    text = "\n".join(parts).strip()
    if not text:
        raise RuntimeError("AI response contained no usable text")
    return text


def _message_usage(message):
    usage = {"input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "cache_creation_input_tokens": getattr(message.usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            "total_tokens": message.usage.input_tokens + message.usage.output_tokens}
    usage["total_tokens"] += usage["cache_creation_input_tokens"] + usage["cache_read_input_tokens"]
    return usage


def _get_client() -> anthropic.Anthropic:
    """Lazy singleton — avoids failing app startup when ANTHROPIC_API_KEY isn't set yet."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=0)
    return _client


@trace_function('ai.claude_html')
def generate_html(
    prompt: str, model: str = "claude-sonnet-4-6", max_tokens: int = 16000
) -> Tuple[str, Dict[str, int]]:
    """
    Blocking call — run via asyncio.to_thread() from async callers.
    Streamed: a full styled HTML document can be tens of thousands of tokens,
    and a non-streaming request with a large max_tokens risks an HTTP read
    timeout. Returns (html_content, token_usage) with any accidental markdown
    fences stripped. If the model hit the token cap, a visible truncation
    banner is appended and a warning is logged.
    """
    token_usage = None
    try:
        with _get_client().messages.stream(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()

        token_usage = _message_usage(message)

        html_content = _response_text(message)
        html_content = re.sub(r"^```html\s*", "", html_content, flags=re.IGNORECASE)
        html_content = re.sub(r"^```\s*", "", html_content)
        html_content = re.sub(r"\s*```\s*$", "", html_content)
        html_content = html_content.strip()

        if message.stop_reason == "max_tokens":
            logging.warning(
                "generate_html: output truncated at max_tokens=%d (model=%s) — raise the caller's budget",
                max_tokens, model,
            )
            html_content += (
                '\n<div style="margin:24px;padding:12px 16px;border:1px solid #f59e0b;'
                'background:#fffbeb;color:#92400e;border-radius:6px;font-family:Arial,sans-serif;'
                'font-size:13px">This document was cut off before it finished generating. '
                'Try a shorter length, or regenerate.</div>'
            )

        return html_content, token_usage

    except Exception as e:
        error = RuntimeError(f"Content generation failed: {e}")
        error.usage = token_usage
        raise error from e


@trace_function('ai.claude_text')
def generate_text(
    prompt: str, model: str = "claude-sonnet-4-6", max_tokens: int = 8000, *, system: str = None
) -> Tuple[str, Dict[str, int]]:
    """
    Blocking call — run via asyncio.to_thread() from async callers.
    Like generate_html() but returns the raw response text unmodified (no markdown-fence
    stripping) — used where the output is plain text, not an HTML document.
    """
    token_usage = None
    try:
        message = _get_client().messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            **({"system": system} if system else {}),
        )

        token_usage = _message_usage(message)
        if getattr(message, "stop_reason", None) in {"max_tokens", "refusal"}:
            raise RuntimeError("AI text output is incomplete or refused; it cannot be published")
        text = _response_text(message)

        return text, token_usage

    except Exception as e:
        error = RuntimeError(f"Content generation failed: {e}")
        error.usage = token_usage
        raise error from e
