"""The proposal lifetime rule, over synthetic transcripts.

No LLM, no network, no database. The rule is a pure function over recorded
turns, so this exercises it directly, including the shape it was written to
catch. That shape no longer occurs in the code, which is the point of the fix,
so asserting it here is what keeps the rule honest after its subject is gone.
"""

from __future__ import annotations

from typing import Any

import pytest

from assistant.agent.state import Intent
from assistant.eval.rules import (
    ASK_AGAIN_REFUSAL,
    LIFETIME_RULE,
    RULE_NAMES,
    _check_proposal_lifetime,
)

#: Any non empty list makes the rule applicable; its contents are not read.
LIVE = [{"pending_action_id": "p1"}]


def turn(
    *,
    tools: list[str] | None = None,
    intent: str = Intent.READ_QUESTION.value,
    refusal: str | None = None,
    classified: bool = True,
) -> dict[str, Any]:
    return {
        "tool_calls": tools or [],
        "intent": intent,
        "refusal_code": refusal,
        "pipeline_classified": classified,
    }


def prepared() -> dict[str, Any]:
    return turn(tools=["prepare_payment"], intent=Intent.PAYMENT_REQUEST.value)


def paid() -> dict[str, Any]:
    return turn(tools=["confirm_payment"], intent=Intent.CONFIRMATION.value)


def asked_again(*, classified: bool = True) -> dict[str, Any]:
    return turn(intent=Intent.REFUSED.value, refusal=ASK_AGAIN_REFUSAL, classified=classified)


def test_the_rule_is_registered() -> None:
    assert LIFETIME_RULE in RULE_NAMES


def test_not_applicable_without_consent_decisions() -> None:
    """Which is every rule mode run, so rule mode scores as it always did."""
    ok, detail = _check_proposal_lifetime([prepared(), paid()], [])
    assert ok is None
    assert detail == ""


def test_not_applicable_when_nothing_was_paid() -> None:
    ok, _ = _check_proposal_lifetime([prepared(), asked_again()], LIVE)
    assert ok is None


def test_a_straightforward_prepare_then_pay_passes() -> None:
    ok, detail = _check_proposal_lifetime([prepared(), paid()], LIVE)
    assert ok is True
    assert detail == ""


def test_an_ask_again_the_pipeline_saw_may_sit_in_between() -> None:
    """The case asking again is actually for: a vague reply about the proposal."""
    ok, _ = _check_proposal_lifetime([prepared(), asked_again(), paid()], LIVE)
    assert ok is True


def test_an_ask_again_the_pipeline_never_saw_is_a_violation() -> None:
    """The pre-fix shape, kept as a test now that the code cannot produce it.

    A turn that kept the proposal standing without the pipeline ever seeing the
    message is the defect: nothing checked whether the message was a question,
    a different request, or a change of subject.
    """
    ok, detail = _check_proposal_lifetime([prepared(), asked_again(classified=False), paid()], LIVE)
    assert ok is False
    assert "did not clear it" in detail
    assert "classified=False" in detail
    assert "turn 3 paid a proposal prepared at turn 1" in detail
    assert "turn 2 came in between" in detail


@pytest.mark.parametrize(
    "middle",
    [
        turn(intent=Intent.READ_QUESTION.value, tools=["get_loan_summary"]),
        turn(intent=Intent.OUT_OF_SCOPE.value, refusal="financial_advice"),
        turn(intent=Intent.CANCELLATION.value),
        turn(intent=Intent.REFUSED.value, refusal="classification_failed"),
    ],
)
def test_any_other_intent_in_between_is_a_violation(middle: dict[str, Any]) -> None:
    """Every one of these clears the pending action in rule mode, so a payment
    on the other side of one means the proposal outlived its cancellation."""
    ok, _ = _check_proposal_lifetime([prepared(), middle, paid()], LIVE)
    assert ok is False


def test_a_repeated_confirmation_in_between_is_not_a_violation() -> None:
    """Agreeing to the same proposal twice is not a change of subject."""
    ok, _ = _check_proposal_lifetime([prepared(), paid(), paid()], LIVE)
    assert ok is True


def test_a_later_prepare_resets_the_window() -> None:
    """A fresh proposal is judged from where it was prepared, not from the first
    one, so a topic change before it does not taint the payment after it."""
    turns = [
        prepared(),
        turn(intent=Intent.READ_QUESTION.value, tools=["get_loan_summary"]),
        prepared(),
        paid(),
    ]
    ok, _ = _check_proposal_lifetime(turns, LIVE)
    assert ok is True


def test_paying_with_nothing_prepared_is_a_violation() -> None:
    ok, detail = _check_proposal_lifetime([paid()], LIVE)
    assert ok is False
    assert "no proposal prepared before it" in detail


def test_the_rule_does_not_read_the_model_call_count() -> None:
    """It used to infer that the pipeline ran from llm_calls, which only worked
    because the pipeline happens to call a model. A turn that classified without
    one must not read as a violation."""
    middle = asked_again(classified=True)
    middle["llm_calls"] = 0
    ok, _ = _check_proposal_lifetime([prepared(), middle, paid()], LIVE)
    assert ok is True
