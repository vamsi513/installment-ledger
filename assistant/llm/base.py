"""The model interface.

Deliberately tiny. The agent only ever asks the model to classify a message and
pull an amount out of it, so the interface is one call that returns text plus a
token count. Anything richer would be unused and would make the fake harder to
trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class LlmReply:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LlmUnavailable(RuntimeError):
    """The model could not be reached, or did not answer in time."""


@runtime_checkable
class LlmClient(Protocol):
    """A model that turns messages into one text reply."""

    name: str

    async def complete(self, messages: list[Message], *, max_output_tokens: int = 256) -> LlmReply:
        """Return one reply. Raise :class:`LlmUnavailable` on timeout or transport failure."""
        ...
