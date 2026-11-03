"""LLM analysis for the Daily Market Report.

Loads the canonical ``prompts/unusual-whales/daily-report.md`` prompt, hands it
the UW digest JSON, and returns the model's plain-text analysis. This is the
only place that talks to an LLM provider, so swapping providers later means
editing just this module (the model name is the ``TF_LLM_MODEL`` config knob).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from app.config import BACKEND_DIR, get_settings

# prompts/ lives at the repo root, one level above backend/.
PROMPT_PATH = BACKEND_DIR.parent / "prompts" / "unusual-whales" / "daily-report.md"
MAX_TOKENS = 2000
# The SDK default is a 600s read timeout with 2 retries, so one stalled response
# could hold a watchlist refresh for ~30 minutes.
REQUEST_TIMEOUT_SECONDS = 120.0
MAX_RETRIES = 1


class LLMError(RuntimeError):
    """Raised when the analysis cannot be produced (missing key, empty output)."""


class LLMClientProtocol(Protocol):
    def analyze(self, prompt: str, model: str) -> str: ...


def load_prompt(path: Path | None = None) -> str:
    return (path or PROMPT_PATH).read_text(encoding="utf-8").strip()


def _build_message(data: dict[str, Any], prompt: str) -> str:
    return f"{prompt}\n\n```json\n{json.dumps(data, indent=2, default=str)}\n```"


class AnthropicClient:
    """Thin wrapper over the Anthropic Messages API."""

    def __init__(self, api_key: str) -> None:
        # Import lazily so the package is only required when actually calling out.
        import anthropic

        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES
        )

    def analyze(self, prompt: str, model: str) -> str:
        message = self._client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [
            block.text
            for block in message.content
            if getattr(block, "type", None) == "text"
        ]
        return "\n".join(parts).strip()


def run(
    prompt: str,
    *,
    model: str | None = None,
    client: LLMClientProtocol | None = None,
) -> str:
    """Run an arbitrary prompt and return the model's text.

    The single place that instantiates a provider client + enforces the key
    check, so callers (daily report, SEC significance, ...) stay provider-
    agnostic. Raises ``LLMError`` on a missing key or empty output.
    """
    settings = get_settings()
    active = client
    if active is None:
        key = (settings.anthropic_api_key or "").strip()
        if not key:
            raise LLMError("Anthropic API key is not configured (set TF_ANTHROPIC_API_KEY).")
        active = AnthropicClient(key)

    text = active.analyze(prompt, model or settings.llm_model)
    if not text.strip():
        raise LLMError("LLM returned an empty analysis.")
    return text


def analyze(
    data: dict[str, Any],
    *,
    model: str | None = None,
    client: LLMClientProtocol | None = None,
) -> str:
    """Run the daily-report prompt over ``data`` and return the analysis text."""
    return run(_build_message(data, load_prompt()), model=model, client=client)
