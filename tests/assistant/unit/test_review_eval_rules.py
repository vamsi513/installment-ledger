"""The review rule in the eval harness.

No LLM, no network, database required. These drive the rule with hand built
audit trails, which is the only way to pin down the orderings it is meant to
catch.
"""

from __future__ import annotations

from typing import Any

import pytest

from assistant.eval.rules import RULE_NAMES, score_scenario

RULE = "review_gate_respected"
HELD = "aaaaaaaa1111"
OTHER = "bbbbbbbb2222"


def entry(action: str, pending_action_id: str = HELD) -> dict[str, Any]:
    return {"action": action, "pending_action_id": pending_action_id}


def scenario(*, review: bool = True, expect_held: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "s",
        "category": "review_held",
        "turns": [{"message": "Pay 500 dollars."}],
        "expect_final": {"payment_count": 0, "paid_cents": 0},
    }
    if review:
        body["review"] = True
    if not expect_held:
        body["expect_held"] = False
    return body


TURNS = [
    {
        "message": "Pay 500 dollars.",
        "tool_calls": ["prepare_payment"],
        "outcome": "held",
        "refusal_code": None,
        "llm_calls": 1,
        "has_pending_action": True,
    }
]
FINAL_CLEAN = {"payment_count": 0, "paid_cents": 0, "ledger_net_cents": 0, "loan_status": "open"}


def verdict(audit: list[dict[str, Any]], **kwargs: Any) -> str:
    score = score_scenario(scenario(**kwargs), TURNS, FINAL_CLEAN, audit)
    return score.rules[RULE]


def test_the_rule_is_part_of_the_published_set() -> None:
    assert RULE in RULE_NAMES


def test_a_scenario_that_says_nothing_about_review_is_not_checked() -> None:
    """This is what keeps the scenarios written before the queue existed scoring
    exactly as they did."""
    assert verdict([], review=False) == "not_applicable"
    assert verdict([entry("prepared"), entry("confirmed")], review=False) == "not_applicable"


def test_a_held_then_approved_then_confirmed_trail_passes() -> None:
    assert (
        verdict(
            [
                entry("prepared"),
                entry("held_for_review"),
                entry("approved"),
                entry("confirmed"),
            ]
        )
        == "pass"
    )


def test_a_held_payment_that_was_never_confirmed_passes() -> None:
    assert (
        verdict([entry("prepared"), entry("held_for_review"), entry("confirm_refused")]) == "pass"
    )


def test_a_rejected_payment_that_was_never_confirmed_passes() -> None:
    assert (
        verdict(
            [
                entry("prepared"),
                entry("held_for_review"),
                entry("rejected"),
                entry("confirm_refused"),
            ]
        )
        == "pass"
    )


def test_a_held_payment_confirmed_without_an_approval_fails() -> None:
    assert verdict([entry("prepared"), entry("held_for_review"), entry("confirmed")]) == "fail"


def test_a_confirmation_before_its_approval_fails() -> None:
    assert (
        verdict(
            [
                entry("prepared"),
                entry("held_for_review"),
                entry("confirmed"),
                entry("approved"),
            ]
        )
        == "fail"
    )


def test_a_rejected_payment_that_was_confirmed_fails() -> None:
    assert (
        verdict(
            [
                entry("prepared"),
                entry("held_for_review"),
                entry("rejected"),
                entry("confirmed"),
            ]
        )
        == "fail"
    )


def test_two_confirmations_of_one_payment_fail() -> None:
    assert (
        verdict(
            [
                entry("prepared"),
                entry("held_for_review"),
                entry("approved"),
                entry("confirmed"),
                entry("confirmed"),
            ]
        )
        == "fail"
    )


def test_a_replayed_confirmation_is_not_a_second_one() -> None:
    """confirmed_replay is what a retry of the same key records, and the ledger
    applied the payment once, so it must not read as two effects."""
    assert (
        verdict(
            [
                entry("prepared"),
                entry("held_for_review"),
                entry("approved"),
                entry("confirmed"),
                entry("confirmed_replay"),
                entry("confirmed_replay"),
            ]
        )
        == "pass"
    )


def test_a_rejection_on_one_payment_says_nothing_about_another() -> None:
    """The trail is read per prepared payment, not per loan.

    One loan can carry a rejected payment and a separate payment that went
    through normally, which is exactly what the "rejected then a smaller
    payment" scenario does.
    """
    assert (
        verdict(
            [
                entry("prepared", HELD),
                entry("held_for_review", HELD),
                entry("rejected", HELD),
                entry("confirm_refused", HELD),
                entry("prepared", OTHER),
                entry("confirmed", OTHER),
            ]
        )
        == "pass"
    )


def test_a_review_scenario_where_nothing_was_held_fails() -> None:
    """A review scenario that never trips the policy is not testing anything."""
    assert verdict([entry("prepared"), entry("confirmed")]) == "fail"


def test_unless_the_scenario_says_it_expects_no_hold() -> None:
    assert verdict([entry("prepared"), entry("confirmed")], expect_held=False) == "pass"


@pytest.mark.parametrize(
    "audit",
    [
        [entry("prepared"), entry("held_for_review"), entry("confirmed")],
        [entry("prepared"), entry("held_for_review"), entry("rejected"), entry("confirmed")],
    ],
)
def test_a_failing_review_rule_fails_the_scenario(audit: list[dict[str, Any]]) -> None:
    score = score_scenario(scenario(), TURNS, FINAL_CLEAN, audit)
    assert score.passed is False
    assert any(RULE in failure for failure in score.failures)
