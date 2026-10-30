"""Running one turn.

A turn opens one MCP tool session, puts it and the model client on a context
variable, runs the graph against the session's checkpoint thread, and closes the
session. One session per turn keeps the anyio cancel scope inside a single task
and keeps concurrent turns off each other's transport.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ..config import Settings, get_settings
from ..llm import LlmClient, build_llm_client
from ..mcp_client import ToolGateway
from ..redaction import redact_session_id
from .graph import build_graph
from .runtime import TurnContext, use_context
from .state import Outcome

logger = logging.getLogger("assistant.turn")

CHECKPOINT_TABLE_SUFFIX = "-checkpoints"


@dataclass(frozen=True)
class TurnResult:
    """What one turn produced. No secret and no full loan id is in here."""

    reply: str
    outcome: str
    intent: str
    refusal_code: str | None
    tool_calls: list[str] = field(default_factory=list)
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    has_pending_action: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def wrote_to_ledger(self) -> bool:
        return self.outcome == Outcome.APPLIED.value


def checkpoint_path(settings: Settings) -> str:
    return f"{settings.state_db_path}{CHECKPOINT_TABLE_SUFFIX}"


async def run_turn(
    *,
    session_id: str,
    message: str,
    bound_loan_ids: list[str],
    settings: Settings | None = None,
    llm: LlmClient | None = None,
) -> TurnResult:
    """Run one message through the graph and return what the user should see."""
    resolved = settings or get_settings()
    model = llm or build_llm_client(resolved)
    started = time.perf_counter()

    async with AsyncSqliteSaver.from_conn_string(checkpoint_path(resolved)) as checkpointer:
        compiled = build_graph().compile(checkpointer=checkpointer)
        async with ToolGateway(resolved) as tools:
            with use_context(TurnContext(settings=resolved, llm=model, tools=tools)):
                final: dict[str, Any] = await compiled.ainvoke(
                    {
                        "session_id": session_id,
                        "message": message,
                        "bound_loan_ids": list(bound_loan_ids),
                    },
                    config={"configurable": {"thread_id": session_id}},
                )

    result = TurnResult(
        reply=str(final.get("reply", "")),
        outcome=str(final.get("outcome", Outcome.FAILED.value)),
        intent=str(final.get("intent", "")),
        refusal_code=final.get("refusal_code"),
        tool_calls=list(final.get("tool_calls") or []),
        llm_calls=int(final.get("llm_calls") or 0),
        prompt_tokens=int(final.get("prompt_tokens") or 0),
        completion_tokens=int(final.get("completion_tokens") or 0),
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        has_pending_action=bool(final.get("pending_action_id")),
    )
    logger.info(
        "turn complete",
        extra={
            "session": redact_session_id(session_id),
            "intent": result.intent,
            "outcome": result.outcome,
            "tools": ",".join(result.tool_calls) or "-",
            "llm_calls": result.llm_calls,
            "duration_ms": result.latency_ms,
        },
    )
    return result
