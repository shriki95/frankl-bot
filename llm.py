"""Shared Claude client and request defaults."""
import logging
import os

import anthropic

import db
import usage

logger = logging.getLogger(__name__)

CHAT_MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
CHAT_EFFORT = os.environ.get("CHAT_EFFORT", "low")
# Memory updates and parliament turn-taking are bookkeeping - a small model does them for a fraction of the cost.
MEMORY_MODEL = os.environ.get("MEMORY_MODEL", "claude-haiku-5-5")
MEMORY_EFFORT = os.environ.get("MEMORY_EFFORT", "medium")
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "claude-haiku-5-5")

client = anthropic.AsyncAnthropic(timeout=120.0)


async def create(purpose="other", **params):
    """purpose: where the tokens go in the usage report - a persona key, "parliament" or "memory"."""
    # The beta namespace accepts mid-conversation system messages (the current time and context).
    response = await client.beta.messages.create(**params)
    used = response.usage
    logger.info(f"Claude {response.model}: {response.stop_reason}, input {used.input_tokens} "
                f"+ cached {used.cache_read_input_tokens or 0} + cache write {used.cache_creation_input_tokens or 0}, "
                f"output {used.output_tokens}")
    try:
        await db.call(usage.record, purpose, response.model, used)
    except Exception:
        logger.exception("Recording token usage failed")
    return response


def text_of(response) -> str:
    """The visible text of a response (thinking and tool blocks are skipped)."""
    parts = [block.text.strip() for block in response.content if block.type == "text"]
    return "\n\n".join(part for part in parts if part)
