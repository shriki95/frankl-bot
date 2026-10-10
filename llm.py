"""Shared Claude client and request defaults."""
import os

import anthropic

CHAT_MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
MEMORY_MODEL = os.environ.get("MEMORY_MODEL", CHAT_MODEL)
CHAT_EFFORT = os.environ.get("CHAT_EFFORT", "low")

# Server-side fallback: if a safety classifier declines a request, the API re-runs it
# on a recommended model instead of returning the refusal.
_FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

client = anthropic.AsyncAnthropic(timeout=120.0)


async def create(**params):
    if params["model"] in _FALLBACK_MODELS:
        params.update(betas=[_FALLBACK_BETA], fallbacks="default")
    return await client.beta.messages.create(**params)


def text_of(response) -> str:
    """The visible text of a response (thinking and tool blocks are skipped)."""
    parts = [block.text.strip() for block in response.content if block.type == "text"]
    return "\n\n".join(part for part in parts if part)
