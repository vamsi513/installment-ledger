"""Request and response bodies for the assistant endpoints.

Unknown fields are rejected, matching the ledger's own request models. A
``pending_action_id`` is never returned: it lives in the session's checkpointed
state, so it cannot be replayed by a caller who guessed one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..sessions import MAX_LOANS_PER_SESSION, is_loan_id

MAX_SESSION_ID_CHARS = 128
MAX_MESSAGE_CHARS = 4_000


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=MAX_SESSION_ID_CHARS)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    loan_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_LOANS_PER_SESSION,
        description=(
            "Loans to attach to this session. Sent once; later turns need only the "
            "session id. The agent refuses to touch a loan outside this set."
        ),
    )

    @field_validator("loan_ids")
    @classmethod
    def _check_loan_ids(cls, value: list[str]) -> list[str]:
        bad = [item for item in value if not is_loan_id(item)]
        if bad:
            raise ValueError(f"not loan ids: {bad}")
        return [item.lower() for item in value]


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    reply: str
    intent: str
    outcome: str
    refusal_code: str | None = None
    tool_calls: list[str] = Field(default_factory=list)
    awaiting_confirmation: bool = Field(
        description="True when a prepared payment is waiting for an explicit yes."
    )
    loan_ids: list[str] = Field(description="Every loan this session is bound to.")
