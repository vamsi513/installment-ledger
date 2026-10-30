"""Per turn runtime objects, kept out of the graph state.

The graph's state is checkpointed, so a live model client and a live tool
session must not go in it. They are put on a context variable instead, which
asyncio copies into the tasks LangGraph spawns for each node.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ..config import Settings
from ..llm import LlmClient
from ..mcp_client import ToolGateway


@dataclass(frozen=True)
class TurnContext:
    settings: Settings
    llm: LlmClient
    tools: ToolGateway


_turn_context: ContextVar[TurnContext | None] = ContextVar("assistant_turn_context", default=None)


def current_context() -> TurnContext:
    context = _turn_context.get()
    if context is None:
        raise RuntimeError("no assistant turn context is active")
    return context


@contextmanager
def use_context(context: TurnContext) -> Iterator[TurnContext]:
    token = _turn_context.set(context)
    try:
        yield context
    finally:
        _turn_context.reset(token)
