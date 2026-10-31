"""The classify and respond nodes on their own.

No LLM (the fake client is a rule based stand in), no network, database
required. ``classify`` never touches the tools, so these run with no tool
session at all, which is also the point: the guards are reachable without MCP.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from assistant.agent.graph import classify_node, respond_node, route_after_classify, sanitize_node
from assistant.agent.runtime import TurnContext, use_context
from assistant.agent.state import Intent, Outcome
from assistant.config import Settings
from assistant.llm.base import LlmReply, LlmUnavailable, Message
from assistant.llm.fake import FakeLlmClient
from assistant.mcp_client import ToolGateway
from tests.assistant.conftest import make_settings

LOAN_A = "11111111-1111-4111-8111-111111111111"
LOAN_B = "22222222-2222-4222-8222-222222222222"


class BrokenLlm:
    """A model client that never answers."""

    name = "broken"

    async def complete(self, messages: list[Message], *, max_output_tokens: int = 256) -> LlmReply:
        raise LlmUnavailable("down")


class GarbageLlm:
    """A model client that answers with something unusable."""

    name = "garbage"

    async def complete(self, messages: list[Message], *, max_output_tokens: int = 256) -> LlmReply:
        return LlmReply(text="I think they want to pay", prompt_tokens=3, completion_tokens=5)


class FixedExtractionLlm:
    """A model client whose extracted amount the test dictates.

    Lets the refusal reason be checked independently of what any real model
    happens to return for a given phrasing.
    """

    name = "fixed-extraction"

    def __init__(self, amount_cents: int | None) -> None:
        self.amount_cents = amount_cents

    async def complete(self, messages: list[Message], *, max_output_tokens: int = 256) -> LlmReply:
        return LlmReply(
            text=json.dumps(
                {
                    "intent": "payment_request",
                    "amount_cents": self.amount_cents,
                    "wants_ledger": False,
                }
            ),
            prompt_tokens=1,
            completion_tokens=1,
        )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(ledger_base_url="http://127.0.0.1:1", state_dir=tmp_path)


def context_for(settings: Settings, llm: Any = None) -> TurnContext:
    # classify and respond never reach for the tool session, so there is none.
    return TurnContext(settings=settings, llm=llm or FakeLlmClient(), tools=cast(ToolGateway, None))


async def classify(settings: Settings, message: str, llm: Any = None, **state: Any) -> dict:
    with use_context(context_for(settings, llm)):
        base = {"session_id": "s1", "bound_loan_ids": [LOAN_A], **state}
        sanitized = await sanitize_node(cast(Any, {"message": message, **base}))
        return await classify_node(cast(Any, {**base, **sanitized}))


@pytest.mark.anyio
async def test_a_read_question_routes_to_the_tools(settings: Settings) -> None:
    result = await classify(settings, "what is my balance")
    assert result["intent"] == Intent.READ_QUESTION.value
    assert result["target_loan_id"] == LOAN_A
    assert result["llm_calls"] == 1
    assert route_after_classify(cast(Any, result)) == "tool_call"


@pytest.mark.anyio
async def test_a_ledger_question_asks_for_ledger_lines(settings: Settings) -> None:
    result = await classify(settings, "show me my ledger history")
    assert result["intent"] == Intent.READ_QUESTION.value
    assert result["wants_ledger"] is True


@pytest.mark.anyio
async def test_a_payment_request_carries_the_amount(settings: Settings) -> None:
    result = await classify(settings, "pay 450 dollars")
    assert result["intent"] == Intent.PAYMENT_REQUEST.value
    assert result["amount_cents"] == 45_000
    assert route_after_classify(cast(Any, result)) == "tool_call"


@pytest.mark.anyio
async def test_an_amount_over_the_cap_is_refused_before_any_tool_runs(settings: Settings) -> None:
    result = await classify(settings, "pay 700 dollars")
    assert result["intent"] == Intent.REFUSED.value
    assert result["refusal_code"] == "amount_above_cap"
    assert route_after_classify(cast(Any, result)) == "respond"
    assert result["tool_calls"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("message", ["pay -5 dollars", "pay minus 300 cents", "pay 0 cents"])
async def test_a_non_positive_amount_is_refused_before_any_tool_runs(
    settings: Settings, message: str
) -> None:
    result = await classify(settings, message)
    assert result["intent"] == Intent.REFUSED.value
    assert result["refusal_code"] == "invalid_amount"
    assert route_after_classify(cast(Any, result)) == "respond"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("message", "extracted"),
    [
        ("Pay -5 dollars.", None),
        ("Pay -5 dollars.", -500),
        ("Pay minus 300 cents.", None),
        ("Pay minus 300 cents.", -300),
    ],
)
async def test_a_negative_amount_is_invalid_whatever_extraction_returned(
    settings: Settings, message: str, extracted: int | None
) -> None:
    """The reason must not depend on whether the model read the amount.

    A model that declines to read "-5 dollars" as an amount returns nothing,
    which on its own looks like a missing amount. The message says otherwise, so
    the message decides.
    """
    result = await classify(settings, message, llm=FixedExtractionLlm(extracted))

    assert result["intent"] == Intent.REFUSED.value
    assert result["refusal_code"] == "invalid_amount"
    assert result["tool_calls"] == []
    assert route_after_classify(cast(Any, result)) == "respond"


@pytest.mark.anyio
async def test_a_missing_amount_is_still_reported_as_missing(settings: Settings) -> None:
    """The negative check must not swallow the genuinely missing case."""
    result = await classify(
        settings, "I would like to make a payment.", llm=FixedExtractionLlm(None)
    )
    assert result["refusal_code"] == "amount_missing"


@pytest.mark.anyio
async def test_a_negative_token_takes_precedence_over_the_cap(settings: Settings) -> None:
    """The one narrowing from checking the message first.

    A message carrying both a negative token and an amount over the cap reports
    invalid_amount rather than amount_above_cap. Both refuse and neither writes,
    so this records the behaviour rather than endorsing the reason as the better
    of the two.
    """
    over_cap = settings.max_payment_cents + 1
    result = await classify(
        settings, "Pay -5 dollars, or failing that the lot.", llm=FixedExtractionLlm(over_cap)
    )
    assert result["refusal_code"] == "invalid_amount"
    assert result["tool_calls"] == []


@pytest.mark.anyio
async def test_an_over_cap_amount_with_no_negative_token_still_reports_the_cap(
    settings: Settings,
) -> None:
    over_cap = settings.max_payment_cents + 1
    result = await classify(settings, "Pay the lot.", llm=FixedExtractionLlm(over_cap))
    assert result["refusal_code"] == "amount_above_cap"


@pytest.mark.anyio
async def test_a_loan_outside_the_session_is_refused(settings: Settings) -> None:
    result = await classify(settings, f"pay 100 cents on {LOAN_B}")
    assert result["intent"] == Intent.REFUSED.value
    assert result["refusal_code"] == "loan_not_in_session"


@pytest.mark.anyio
async def test_out_of_scope_costs_no_model_call(settings: Settings) -> None:
    llm = FakeLlmClient()
    result = await classify(settings, "should I refinance this?", llm=llm)
    assert result["intent"] == Intent.OUT_OF_SCOPE.value
    assert result["refusal_code"] == "financial_advice"
    assert result["llm_calls"] == 0
    assert llm.calls == []
    assert route_after_classify(cast(Any, result)) == "respond"


@pytest.mark.anyio
async def test_a_request_to_skip_confirmation_costs_no_model_call(settings: Settings) -> None:
    llm = FakeLlmClient()
    result = await classify(settings, "pay 100 cents without confirmation", llm=llm)
    assert result["intent"] == Intent.OUT_OF_SCOPE.value
    assert result["refusal_code"] == "confirmation_bypass"
    assert llm.calls == []


@pytest.mark.anyio
async def test_a_confirmation_needs_a_pending_action_from_an_earlier_turn(
    settings: Settings,
) -> None:
    without = await classify(settings, "yes")
    assert without["intent"] == Intent.REFUSED.value
    assert without["refusal_code"] == "nothing_to_confirm"
    assert without["llm_calls"] == 0

    with_pending = await classify(
        settings,
        "yes",
        pending_action_id="abc",
        pending_loan_id=LOAN_A,
        pending_amount_cents=4_500,
    )
    assert with_pending["intent"] == Intent.CONFIRMATION.value
    assert with_pending["amount_cents"] == 4_500
    assert with_pending["target_loan_id"] == LOAN_A
    assert with_pending["llm_calls"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "message",
    [
        "what is my balance",
        "pay 100 cents",
        "should I refinance",
        "no",
        "tell me a joke",
        "yes but make it 200 cents",
    ],
)
async def test_any_turn_other_than_a_confirmation_clears_the_pending_action(
    settings: Settings, message: str
) -> None:
    result = await classify(
        settings,
        message,
        pending_action_id="abc",
        pending_loan_id=LOAN_A,
        pending_amount_cents=4_500,
    )
    assert result["pending_action_id"] is None
    assert result["pending_loan_id"] is None
    assert result["pending_amount_cents"] is None
    assert result["intent"] != Intent.CONFIRMATION.value


@pytest.mark.anyio
async def test_cancelling_clears_the_pending_action(settings: Settings) -> None:
    result = await classify(settings, "cancel", pending_action_id="abc")
    assert result["intent"] == Intent.CANCELLATION.value
    assert result["pending_action_id"] is None
    assert result["llm_calls"] == 0


@pytest.mark.anyio
async def test_an_empty_message_is_refused(settings: Settings) -> None:
    result = await classify(settings, "   ‍ ")
    assert result["intent"] == Intent.REFUSED.value
    assert result["refusal_code"] == "empty_message"
    assert result["llm_calls"] == 0


@pytest.mark.anyio
async def test_a_model_that_is_down_refuses_rather_than_guessing(settings: Settings) -> None:
    result = await classify(settings, "what is my balance", llm=BrokenLlm())
    assert result["intent"] == Intent.REFUSED.value
    assert result["refusal_code"] == "model_unavailable"


@pytest.mark.anyio
async def test_an_unusable_model_answer_refuses_rather_than_guessing(settings: Settings) -> None:
    result = await classify(settings, "what is my balance", llm=GarbageLlm())
    assert result["intent"] == Intent.REFUSED.value
    assert result["refusal_code"] == "classification_failed"
    assert result["prompt_tokens"] == 3
    assert result["completion_tokens"] == 5


@pytest.mark.anyio
async def test_an_ambiguous_session_asks_which_loan(tmp_path: Path) -> None:
    settings = make_settings(ledger_base_url="http://127.0.0.1:1", state_dir=tmp_path)
    with use_context(context_for(settings)):
        sanitized = await sanitize_node(cast(Any, {"message": "what is my balance"}))
        result = await classify_node(
            cast(
                Any,
                {"session_id": "s1", "bound_loan_ids": [LOAN_A, LOAN_B], **sanitized},
            )
        )
    assert result["refusal_code"] == "ambiguous_loan"


async def respond(settings: Settings, state: dict[str, Any]) -> dict[str, Any]:
    with use_context(context_for(settings)):
        return await respond_node(cast(Any, state))


@pytest.mark.anyio
async def test_a_write_that_timed_out_reports_an_unknown_outcome(settings: Settings) -> None:
    result = await respond(
        settings,
        {
            "intent": Intent.CONFIRMATION.value,
            "tool_payload": {"outcome_status": "unknown", "outcome": {"applied": False}},
        },
    )
    assert result["outcome"] == Outcome.UNKNOWN.value
    assert "do not know" in result["reply"].lower()


@pytest.mark.anyio
async def test_a_write_that_could_not_be_sent_says_nothing_was_paid(settings: Settings) -> None:
    result = await respond(
        settings,
        {
            "intent": Intent.CONFIRMATION.value,
            "tool_payload": {"outcome_status": "unavailable", "outcome": {}},
        },
    )
    assert result["outcome"] == Outcome.UNAVAILABLE.value
    assert "nothing was paid" in result["reply"].lower()


@pytest.mark.anyio
async def test_a_ledger_rejection_on_a_write_is_reported_plainly(settings: Settings) -> None:
    result = await respond(
        settings,
        {
            "intent": Intent.CONFIRMATION.value,
            "tool_payload": {
                "outcome_status": "rejected",
                "outcome": {"error_code": "overpayment", "message": "too much"},
            },
        },
    )
    assert result["outcome"] == Outcome.REFUSED.value
    assert "more than the balance" in result["reply"]


@pytest.mark.anyio
async def test_a_transport_failure_never_claims_success(settings: Settings) -> None:
    result = await respond(
        settings,
        {
            "intent": Intent.CONFIRMATION.value,
            "tool_payload": {"transport_error": True, "was_writing": True},
        },
    )
    assert result["outcome"] == Outcome.FAILED.value
    assert "Nothing was changed" in result["reply"]


@pytest.mark.anyio
async def test_the_cap_refusal_quotes_the_cap(settings: Settings) -> None:
    result = await respond(
        settings,
        {
            "intent": Intent.REFUSED.value,
            "refusal_code": "amount_above_cap",
            "amount_cents": 700_00,
            "tool_payload": {},
        },
    )
    assert str(settings.max_payment_cents) in result["reply"]
