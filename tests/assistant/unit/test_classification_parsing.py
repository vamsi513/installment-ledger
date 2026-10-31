"""Reading the model's answer. No LLM, no network, database required."""

from __future__ import annotations

import json

import pytest

from assistant.agent.graph import parse_classification


def test_a_well_formed_answer_is_accepted() -> None:
    parsed = parse_classification(
        json.dumps({"intent": "payment_request", "amount_cents": 4500, "wants_ledger": False})
    )
    assert parsed == {"intent": "payment_request", "amount_cents": 4500, "wants_ledger": False}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json at all",
        "[]",
        '"a string"',
        "null",
        json.dumps({"intent": "confirmation", "amount_cents": None}),
        json.dumps({"intent": "transfer_money", "amount_cents": None}),
        json.dumps({"amount_cents": 100}),
        json.dumps({"intent": "payment_request", "amount_cents": "4500"}),
        json.dumps({"intent": "payment_request", "amount_cents": 45.5}),
        json.dumps({"intent": "payment_request", "amount_cents": True}),
    ],
)
def test_anything_unexpected_is_a_failure_rather_than_a_guess(text: str) -> None:
    """A model cannot widen its own remit by returning a different intent."""
    assert parse_classification(text) is None


def test_a_missing_or_wrongly_typed_ledger_flag_defaults_to_false() -> None:
    parsed = parse_classification(json.dumps({"intent": "read_question", "amount_cents": None}))
    assert parsed is not None
    assert parsed["wants_ledger"] is False

    parsed = parse_classification(
        json.dumps({"intent": "read_question", "amount_cents": None, "wants_ledger": "yes"})
    )
    assert parsed is not None
    assert parsed["wants_ledger"] is False
