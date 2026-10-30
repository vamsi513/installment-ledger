"""The OpenAI implementation of :class:`~assistant.llm.base.LlmClient`.

The key is read from the environment by :mod:`assistant.config` and passed in
here. It is never logged, never put in an error message and never returned to a
caller.
"""

from __future__ import annotations

import logging

from .base import LlmReply, LlmUnavailable, Message

logger = logging.getLogger("assistant.llm.openai")


class OpenAiLlmClient:
    """One chat completion per call, with JSON output requested explicitly."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 20.0,
        json_only: bool = True,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise LlmUnavailable(
                "The openai package is not installed. Install the 'assistant' extra."
            ) from exc

        self.model = model
        self._json_only = json_only
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    async def complete(self, messages: list[Message], *, max_output_tokens: int = 256) -> LlmReply:
        request: dict[str, object] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_completion_tokens": max_output_tokens,
            "temperature": 0,
        }
        if self._json_only:
            request["response_format"] = {"type": "json_object"}

        try:
            response = await self._client.chat.completions.create(**request)  # type: ignore[arg-type]
        except Exception as exc:
            # The message is logged without the request body, which carries user
            # text, and without anything derived from the key.
            logger.warning("model call failed", extra={"error_type": type(exc).__name__})
            raise LlmUnavailable(f"the model could not be reached ({type(exc).__name__})") from exc

        choice = response.choices[0] if response.choices else None
        text = (choice.message.content if choice and choice.message else None) or ""
        usage = response.usage
        return LlmReply(
            text=text,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=getattr(response, "model", self.model),
        )
