"""Shared Claude client and request defaults."""
import logging
import os

import anthropic

logger = logging.getLogger(__name__)

CHAT_MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
MEMORY_MODEL = os.environ.get("MEMORY_MODEL", CHAT_MODEL)
CHAT_EFFORT = os.environ.get("CHAT_EFFORT", "low")

client = anthropic.AsyncAnthropic(timeout=120.0)


async def create(**params):
    # The beta namespace accepts mid-conversation system messages (the current time and context).
    response = await client.beta.messages.create(**params)
    usage = response.usage
    logger.info(f"Claude {response.model}: {response.stop_reason}, input {usage.input_tokens} "
                f"+ cached {usage.cache_read_input_tokens or 0} + cache write {usage.cache_creation_input_tokens or 0}, "
                f"output {usage.output_tokens}")
    return response


def text_of(response) -> str:
    """The visible text of a response (thinking and tool blocks are skipped)."""
    parts = [block.text.strip() for block in response.content if block.type == "text"]
    return "\n\n".join(part for part in parts if part)
