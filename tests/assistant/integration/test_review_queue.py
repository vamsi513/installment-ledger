"""The review queue end to end.

Real Postgres, a real ledger over loopback, a real MCP subprocess, and the fake
model client. The queue is switched on through settings rather than through the
environment, so these tests cannot leak the policy into any other test.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import Engine, text

from assistant.agent import run_turn
from assistant.agent.state import Outcome
from assistant.config import Settings
from assistant.mcp_client import ToolGateway
from assistant.pending import ActionStatus
from assistant.review import ReviewService
from assistant.review.audit import AuditAction
from tests.assistant.conftest import TEST_REVIEWER_ID
from tests.helpers import create_loan

UNDER_THRESHOLD_CENTS = 30_000
OVER_THRESHOLD_CENTS = 50_000


def payments(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT COUNT(*) FROM payments")).scalar_one())


def unbalanced(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT transaction_id FROM ledger_entries GROUP BY transaction_id
                HAVING SUM(CASE WHEN direction = 'debit'
                                THEN amount_cents ELSE -amount_cents END) <> 0
                """
            )
        ).all()
    return [str(row[0]) for row in rows]


async def prepare(settings: Settings, loan_id: str, amount_cents: int):
    async with ToolGateway(settings) as tools:
        return await tools.call(
            "prepare_payment", {"loan_id": loan_id, "amount_cents": amount_cents}
        )


async def confirm(settings: Settings, pending_action_id: str):
    async with ToolGateway(settings) as tools:
        return await tools.call("confirm_payment", {"pending_action_id": pending_action_id})


@pytest.mark.anyio
async def test_a_small_payment_is_not_held(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    preview = await prepare(review_settings, loan["id"], UNDER_THRESHOLD_CENTS)

    assert preview.status == "ok"
    assert preview.payload["review_status"] == ActionStatus.READY.value
    assert preview.payload["review_reasons"] == []

    outcome = await confirm(review_settings, preview.payload["pending_action_id"])
    assert outcome.status == "ok"
    assert outcome.payload["applied"] is True
    assert payments(engine) == 1


@pytest.mark.anyio
async def test_a_large_payment_is_held_and_cannot_be_confirmed(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    preview = await prepare(review_settings, loan["id"], OVER_THRESHOLD_CENTS)

    assert preview.status == "ok"
    assert preview.payload["review_status"] == ActionStatus.NEEDS_REVIEW.value
    assert preview.payload["review_reasons"] == ["amount_above_threshold"]

    pending_id = preview.payload["pending_action_id"]
    outcome = await confirm(review_settings, pending_id)

    assert outcome.status == "rejected"
    assert outcome.payload["error_code"] == "awaiting_review"
    assert outcome.payload["applied"] is False
    assert outcome.payload["review_status"] == ActionStatus.NEEDS_REVIEW.value

    # Nothing reached the ledger at all.
    assert payments(engine) == 0
    assert client.get(f"/loans/{loan['id']}").json()["paid_cents"] == 0

    actions = [entry.action for entry in ReviewService(review_settings).entries_for(pending_id)]
    assert actions == [
        AuditAction.PREPARED,
        AuditAction.HELD_FOR_REVIEW,
        AuditAction.CONFIRM_REFUSED,
    ]


@pytest.mark.anyio
async def test_approving_lets_the_confirmation_through(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    preview = await prepare(review_settings, loan["id"], OVER_THRESHOLD_CENTS)
    pending_id = preview.payload["pending_action_id"]
    service = ReviewService(review_settings)

    assert [item.pending_action_id for item in service.held()] == [pending_id]

    result = service.approve(pending_id, actor=TEST_REVIEWER_ID, reason="spoke to the borrower")
    assert result.outcome.value == "approved"
    assert result.changed is True
    assert service.held() == []

    outcome = await confirm(review_settings, pending_id)
    assert outcome.status == "ok"
    assert outcome.payload["applied"] is True
    assert outcome.payload["review_status"] == ActionStatus.APPROVED.value

    assert payments(engine) == 1
    assert client.get(f"/loans/{loan['id']}").json()["paid_cents"] == OVER_THRESHOLD_CENTS
    assert unbalanced(engine) == []

    trail = service.entries_for(pending_id)
    assert [entry.action for entry in trail] == [
        AuditAction.PREPARED,
        AuditAction.HELD_FOR_REVIEW,
        AuditAction.APPROVED,
        AuditAction.CONFIRMED,
    ]
    approval = trail[2]
    assert approval.actor == TEST_REVIEWER_ID
    assert approval.old_status == "needs_review"
    assert approval.new_status == "approved"
    assert approval.reason == "spoke to the borrower"
    assert approval.amount_cents == OVER_THRESHOLD_CENTS
    assert approval.loan_id == loan["id"]


@pytest.mark.anyio
async def test_rejecting_keeps_the_payment_out_of_the_ledger_for_good(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    preview = await prepare(review_settings, loan["id"], OVER_THRESHOLD_CENTS)
    pending_id = preview.payload["pending_action_id"]
    service = ReviewService(review_settings)

    rejected = service.reject(pending_id, actor=TEST_REVIEWER_ID, reason="borrower did not ask")
    assert rejected.outcome.value == "rejected"

    outcome = await confirm(review_settings, pending_id)
    assert outcome.status == "rejected"
    assert outcome.payload["error_code"] == "review_rejected"
    assert payments(engine) == 0

    # And it cannot be approved afterwards.
    assert service.approve(pending_id, actor=TEST_REVIEWER_ID).outcome.value == "not_held"
    assert (await confirm(review_settings, pending_id)).payload["error_code"] == "review_rejected"
    assert payments(engine) == 0


@pytest.mark.anyio
async def test_too_many_prepares_on_one_loan_are_held(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    """The limit in the test settings is three, so the fourth is held."""
    loan = create_loan(client, principal_cents=120_000, installment_count=4)

    async with ToolGateway(review_settings) as tools:
        statuses = []
        for _ in range(4):
            preview = await tools.call(
                "prepare_payment", {"loan_id": loan["id"], "amount_cents": 1_000}
            )
            statuses.append(preview.payload["review_status"])
            last = preview.payload

    assert statuses == ["ready", "ready", "ready", "needs_review"]
    assert last["review_reasons"] == ["too_many_prepares_in_window"]
    assert payments(engine) == 0


@pytest.mark.anyio
async def test_a_separate_loan_has_its_own_prepare_count(
    client: httpx.Client, review_settings: Settings
) -> None:
    first = create_loan(client, principal_cents=120_000, installment_count=4)
    second = create_loan(client, principal_cents=120_000, installment_count=4)

    async with ToolGateway(review_settings) as tools:
        for _ in range(4):
            await tools.call("prepare_payment", {"loan_id": first["id"], "amount_cents": 1_000})
        other = await tools.call(
            "prepare_payment", {"loan_id": second["id"], "amount_cents": 1_000}
        )

    assert other.payload["review_status"] == "ready"


@pytest.mark.anyio
async def test_with_the_queue_off_a_large_payment_is_not_held(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """The default. This is the test that the feature is genuinely optional."""
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    preview = await prepare(assistant_settings, loan["id"], OVER_THRESHOLD_CENTS)

    assert preview.payload["review_status"] == ActionStatus.READY.value
    assert preview.payload["review_reasons"] == []

    outcome = await confirm(assistant_settings, preview.payload["pending_action_id"])
    assert outcome.payload["applied"] is True
    assert payments(engine) == 1


@pytest.mark.anyio
async def test_the_tool_server_offers_no_way_to_approve(review_settings: Settings) -> None:
    """The agent must not be able to clear its own hold."""
    async with ToolGateway(review_settings) as tools:
        names = set(tools.tool_names)
    assert names == {
        "get_loan_summary",
        "get_ledger_entries",
        "prepare_payment",
        "confirm_payment",
    }
    assert not any("approve" in name or "review" in name for name in names)


@pytest.mark.anyio
async def test_the_assistant_tells_the_borrower_a_payment_is_held(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    session = "review-held"

    held = await run_turn(
        session_id=session,
        message="pay 500 dollars",
        bound_loan_ids=[loan["id"]],
        settings=review_settings,
    )
    assert held.outcome == Outcome.HELD.value
    assert "needs a reviewer to approve it" in held.reply
    assert "nothing has been paid" in held.reply
    assert "Reply yes to confirm" not in held.reply
    assert held.has_pending_action is True
    assert payments(engine) == 0

    # A yes while it is held is refused, and says why.
    refused = await run_turn(
        session_id=session,
        message="yes",
        bound_loan_ids=[loan["id"]],
        settings=review_settings,
    )
    assert refused.outcome == Outcome.REFUSED.value
    assert "waiting for a reviewer" in refused.reply
    assert payments(engine) == 0


@pytest.mark.anyio
async def test_the_borrower_can_confirm_once_a_reviewer_approves(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    session = "review-approved"

    await run_turn(
        session_id=session,
        message="pay 500 dollars",
        bound_loan_ids=[loan["id"]],
        settings=review_settings,
    )
    service = ReviewService(review_settings)
    held = service.held()
    assert len(held) == 1
    service.approve(held[0].pending_action_id, actor=TEST_REVIEWER_ID, reason="fine")

    applied = await run_turn(
        session_id=session,
        message="yes",
        bound_loan_ids=[loan["id"]],
        settings=review_settings,
    )
    assert applied.outcome == Outcome.APPLIED.value
    assert "Paid 50000 cents" in applied.reply
    assert payments(engine) == 1
    assert unbalanced(engine) == []


@pytest.mark.anyio
async def test_the_borrower_is_told_plainly_when_a_reviewer_rejects(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    session = "review-rejected"

    await run_turn(
        session_id=session,
        message="pay 500 dollars",
        bound_loan_ids=[loan["id"]],
        settings=review_settings,
    )
    service = ReviewService(review_settings)
    held = service.held()
    service.reject(held[0].pending_action_id, actor=TEST_REVIEWER_ID, reason="not recognised")

    refused = await run_turn(
        session_id=session,
        message="yes",
        bound_loan_ids=[loan["id"]],
        settings=review_settings,
    )
    assert refused.outcome == Outcome.REFUSED.value
    assert "rejected that payment" in refused.reply
    assert payments(engine) == 0
