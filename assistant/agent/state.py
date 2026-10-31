"""The state one conversational turn reads and writes.

LangGraph checkpoints this per session, which is how a ``pending_action_id``
survives from the turn that prepared a payment to the turn that confirms it.
Keys absent from a turn's input keep their checkpointed value, so the pending
action has to be cleared on purpose rather than by omission.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict


class Intent(StrEnum):
    READ_QUESTION = "read_question"
    PAYMENT_REQUEST = "payment_request"
    CONFIRMATION = "confirmation"
    CANCELLATION = "cancellation"
    OUT_OF_SCOPE = "out_of_scope"
    REFUSED = "refused"


class Outcome(StrEnum):
    ANSWERED = "answered"
    PREPARED = "prepared"
    APPLIED = "applied"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class AgentState(TypedDict, total=False):
    # Per turn input
    session_id: str
    message: str
    bound_loan_ids: list[str]

    # Per turn working values
    sanitized: str
    intent: str
    refusal_code: str | None
    target_loan_id: str | None
    amount_cents: int | None
    wants_ledger: bool

    # Carried across turns by the checkpointer
    pending_action_id: str | None
    pending_loan_id: str | None
    pending_amount_cents: int | None

    # Per turn results
    tool_calls: list[str]
    tool_payload: dict[str, Any]
    reply: str
    outcome: str

    # Per turn accounting, used by the eval harness
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
