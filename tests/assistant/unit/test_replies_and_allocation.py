"""Reply text and the payment preview. No LLM, no network, database required."""

from __future__ import annotations

from assistant.agent import replies
from assistant.mcp_server.server import plan_allocation
from assistant.redaction import redact_loan_id, redact_session_id

LOAN = "11111111-1111-4111-8111-1234567890ab"


def _installments(*outstanding: int) -> list[dict[str, object]]:
    return [
        {
            "sequence": index + 1,
            "due_date": f"2026-0{index + 1}-15",
            "amount_cents": 30_000,
            "paid_cents": 30_000 - amount,
            "outstanding_cents": amount,
        }
        for index, amount in enumerate(outstanding)
    ]


def test_allocation_fills_the_oldest_unpaid_installment_first() -> None:
    covered = plan_allocation(_installments(30_000, 30_000, 30_000, 30_000), 45_000)
    assert [(item.sequence, item.applied_cents, item.clears_installment) for item in covered] == [
        (1, 30_000, True),
        (2, 15_000, False),
    ]


def test_allocation_skips_installments_already_paid_off() -> None:
    covered = plan_allocation(_installments(0, 0, 30_000, 30_000), 10_000)
    assert [(item.sequence, item.applied_cents) for item in covered] == [(3, 10_000)]


def test_allocation_of_the_whole_balance_covers_every_installment() -> None:
    covered = plan_allocation(_installments(30_000, 30_000), 60_000)
    assert all(item.clears_installment for item in covered)
    assert sum(item.applied_cents for item in covered) == 60_000


def test_allocation_of_nothing_covers_nothing() -> None:
    assert plan_allocation(_installments(30_000), 0) == []


def test_an_unknown_outcome_reply_claims_neither_result() -> None:
    text = replies.unknown_outcome_reply()
    lowered = text.lower()
    # It has to say the outcome is unknown ...
    assert "do not know" in lowered
    assert "whether" in lowered
    # ... not that the payment went through ...
    assert not text.startswith("Paid")
    assert "has been applied" not in lowered
    assert "was successful" not in lowered
    # ... not that it did not ...
    assert "nothing was paid" not in lowered
    # ... and it has to offer the one safe next step.
    assert "check your balance" in lowered
    assert "send it again" in lowered


def test_a_timed_out_write_is_not_described_as_a_failure_either() -> None:
    # Saying "nothing was paid" after a timeout would be a guess in the other
    # direction, so the reply must not say that either.
    assert "nothing was paid" not in replies.unknown_outcome_reply().lower()


def test_an_unavailable_write_does_say_nothing_was_paid() -> None:
    assert "nothing was paid" in replies.unavailable_reply(was_writing=True).lower()


def test_the_applied_reply_uses_the_numbers_the_ledger_returned() -> None:
    text = replies.payment_applied_reply(
        {"amount_cents": 4_500, "outstanding_after_cents": 75_000, "loan_status": "open"}
    )
    assert "4500 cents" in text
    assert "75000 cents" in text
    assert "paid off" not in text


def test_the_applied_reply_mentions_a_closed_loan() -> None:
    text = replies.payment_applied_reply(
        {"amount_cents": 100, "outstanding_after_cents": 0, "loan_status": "closed"}
    )
    assert "paid off" in text


def test_the_preview_reply_says_nothing_has_been_paid_yet() -> None:
    text = replies.payment_preview_reply(
        {
            "amount_cents": 4_500,
            "outstanding_after_cents": 75_000,
            "covers": [
                {"sequence": 1, "applied_cents": 4_500, "clears_installment": False},
            ],
        }
    )
    assert "Nothing has been paid yet" in text
    assert "Reply yes to confirm" in text


def test_every_out_of_scope_reason_has_fixed_text() -> None:
    from assistant.agent.guards import OUT_OF_SCOPE_PATTERNS

    for reason, _ in OUT_OF_SCOPE_PATTERNS:
        assert reason in replies.OUT_OF_SCOPE_REPLIES, reason
        assert replies.OUT_OF_SCOPE_REPLIES[reason]


def test_ledger_rejections_have_their_own_wording() -> None:
    assert "more than the balance" in replies.ledger_rejection_reply("overpayment", None)
    assert "already paid off" in replies.ledger_rejection_reply("loan_closed", None)
    assert "expired" in replies.ledger_rejection_reply("pending_action_expired", None)
    assert "already seen" in replies.ledger_rejection_reply("idempotency_key_conflict", None)
    assert "refused" in replies.ledger_rejection_reply("something_new", "a reason")


def test_loan_ids_reach_logs_as_four_characters() -> None:
    assert redact_loan_id(LOAN) == "...90ab"
    assert LOAN not in redact_loan_id(LOAN)
    assert redact_loan_id("ab") == "...ab"
    assert redact_loan_id(None) == "(none)"
    assert redact_session_id("session-abcdef") == "...cdef"
