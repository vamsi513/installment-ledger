"""Pick an implementation from settings."""

from __future__ import annotations

from ..config import Settings
from .base import LlmClient
from .fake import FakeLlmClient


def build_llm_client(settings: Settings) -> LlmClient:
    """Return the configured model client.

    ``ASSISTANT_LLM=fake`` gives the deterministic stand in, which is what the
    offline tests use. Anything else requires a key.
    """
    if settings.llm_provider == "fake":
        return FakeLlmClient()
    if settings.llm_provider != "openai":
        raise ValueError(
            f"unknown ASSISTANT_LLM {settings.llm_provider!r}; expected 'openai' or 'fake'"
        )

    from .openai_client import OpenAiLlmClient

    return OpenAiLlmClient(
        api_key=settings.require_openai_api_key(),
        model=settings.model,
        timeout_seconds=settings.ledger_timeout_seconds * 2,
    )
