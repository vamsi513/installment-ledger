"""Races around the review step.

The thing being protected is that a payment reaches the ledger at most once,
and never at all while it is held. Review adds a second actor, so there are
three races worth pinning down: two reviewers approving together, two
confirmations arriving together, and an approval arriving at the same moment as
a confirmation.

Each thread gets its own event loop, its own MCP subprocess and its own SQLite
connections, and they are lined up on a barrier so they contend on purpose.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import Engine, text

from assistant.config import Settings
from assistant.mcp_client import ToolCallResult, ToolGateway
from assistant.pending import ActionStatus
from assistant.review import ReviewOutcome, ReviewService
from assistant.review.audit import AuditAction
from tests.assistant.conftest import TEST_REVIEWER_ID
from tests.helpers import create_loan

THREAD_COUNT = 6
HELD_AMOUNT_CENTS = 50_000


def payments(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT COUNT(*) FROM payments")).scalar_one())


def paid_cents(client: httpx.Client, loan_id: str) -> int:
    return int(client.get(f"/loans/{loan_id}").json()["paid_cents"])


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


async def hold_a_payment(settings: Settings, loan_id: str) -> str:
    """Prepare a payment the policy will hold, and return its id."""
    async with ToolGateway(settings) as tools:
        preview = await tools.call(
            "prepare_payment", {"loan_id": loan_id, "amount_cents": HELD_AMOUNT_CENTS}
        )
    assert preview.payload["review_status"] == ActionStatus.NEEDS_REVIEW.value
    return str(preview.payload["pending_action_id"])


async def confirm_once(settings: Settings, pending_action_id: str) -> ToolCallResult:
    """Confirm from the test's own loop.

    The worker threads below each need their own loop and use asyncio.run, but
    the test body already runs inside one, where asyncio.run is an error.
    """
    async with ToolGateway(settings) as tools:
        return await tools.call("confirm_payment", {"pending_action_id": pending_action_id})


@pytest.mark.anyio
async def test_many_reviewers_approving_at_once_make_one_transition(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    pending_id = await hold_a_payment(review_settings, loan["id"])
    barrier = threading.Barrier(THREAD_COUNT)

    def approve(index: int):
        service = ReviewService(review_settings)
        barrier.wait(timeout=60)
        return service.approve(
            pending_id, actor=f"{TEST_REVIEWER_ID}-{index}", reason=f"call {index}"
        )

    with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
        results = list(pool.map(approve, range(THREAD_COUNT)))

    changed = [result for result in results if result.changed]
    assert len(changed) == 1, "exactly one call may perform the transition"
    assert changed[0].outcome is ReviewOutcome.APPROVED
    assert all(
        result.outcome is ReviewOutcome.ALREADY_APPROVED for result in results if not result.changed
    )

    service = ReviewService(review_settings)
    assert service.store.get(pending_id).status is ActionStatus.APPROVED

    # One approval in the trail, not six.
    approvals = [
        entry for entry in service.entries_for(pending_id) if entry.action is AuditAction.APPROVED
    ]
    assert len(approvals) == 1
    assert approvals[0].old_status == "needs_review"
    assert approvals[0].new_status == "approved"

    outcome = await confirm_once(review_settings, pending_id)
    assert outcome.payload["applied"] is True
    assert payments(engine) == 1
    assert paid_cents(client, loan["id"]) == HELD_AMOUNT_CENTS


@pytest.mark.anyio
async def test_many_confirmations_of_one_approved_payment_post_once(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    pending_id = await hold_a_payment(review_settings, loan["id"])
    ReviewService(review_settings).approve(pending_id, actor=TEST_REVIEWER_ID, reason="fine")

    barrier = threading.Barrier(THREAD_COUNT)

    def confirm(_: int) -> ToolCallResult:
        async def once() -> ToolCallResult:
            async with ToolGateway(review_settings) as tools:
                barrier.wait(timeout=60)
                return await tools.call("confirm_payment", {"pending_action_id": pending_id})

        return asyncio.run(once())

    with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
        results = list(pool.map(confirm, range(THREAD_COUNT)))

    assert [result.status for result in results] == ["ok"] * THREAD_COUNT
    assert len({result.payload["payment_id"] for result in results}) == 1

    assert payments(engine) == 1
    assert paid_cents(client, loan["id"]) == HELD_AMOUNT_CENTS
    assert unbalanced(engine) == []

    # One of them was the first; the rest are recorded as replays.
    trail = ReviewService(review_settings).entries_for(pending_id)
    assert len([e for e in trail if e.action is AuditAction.CONFIRMED]) == 1
    assert len([e for e in trail if e.action is AuditAction.CONFIRMED_REPLAY]) == THREAD_COUNT - 1


@pytest.mark.anyio
async def test_an_approval_racing_a_confirmation_posts_at_most_once(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    """Either the confirmation arrives first and is refused, or it arrives after
    the approval and posts. Both orders are fine; two payments would not be."""
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    pending_id = await hold_a_payment(review_settings, loan["id"])
    barrier = threading.Barrier(2)

    def approve():
        service = ReviewService(review_settings)
        barrier.wait(timeout=60)
        return service.approve(pending_id, actor=TEST_REVIEWER_ID, reason="racing")

    def confirm() -> ToolCallResult:
        async def once() -> ToolCallResult:
            async with ToolGateway(review_settings) as tools:
                barrier.wait(timeout=60)
                return await tools.call("confirm_payment", {"pending_action_id": pending_id})

        return asyncio.run(once())

    with ThreadPoolExecutor(max_workers=2) as pool:
        approval_future = pool.submit(approve)
        confirm_future = pool.submit(confirm)
        approval = approval_future.result(timeout=120)
        confirmation = confirm_future.result(timeout=120)

    assert approval.outcome is ReviewOutcome.APPROVED
    assert ReviewService(review_settings).store.get(pending_id).status is ActionStatus.APPROVED

    # Whichever way the race went, the ledger holds no more than one payment.
    assert payments(engine) <= 1
    if confirmation.status == "ok":
        assert confirmation.payload["applied"] is True
        assert payments(engine) == 1
    else:
        assert confirmation.payload["error_code"] == "awaiting_review"
        assert confirmation.payload["applied"] is False
        assert payments(engine) == 0

    # And confirming again settles on exactly one, whichever way it went.
    again = await confirm_once(review_settings, pending_id)
    assert again.payload["applied"] is True
    assert payments(engine) == 1
    assert paid_cents(client, loan["id"]) == HELD_AMOUNT_CENTS
    assert unbalanced(engine) == []


@pytest.mark.anyio
async def test_an_approval_racing_a_rejection_settles_on_one_of_them(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    pending_id = await hold_a_payment(review_settings, loan["id"])
    barrier = threading.Barrier(2 * THREAD_COUNT)

    def decide(index: int):
        service = ReviewService(review_settings)
        barrier.wait(timeout=60)
        if index % 2 == 0:
            return service.approve(pending_id, actor=f"approver-{index}", reason="yes")
        return service.reject(pending_id, actor=f"rejecter-{index}", reason="no")

    with ThreadPoolExecutor(max_workers=2 * THREAD_COUNT) as pool:
        results = list(pool.map(decide, range(2 * THREAD_COUNT)))

    changed = [result for result in results if result.changed]
    assert len(changed) == 1, "only one decision may take effect"

    service = ReviewService(review_settings)
    final = service.store.get(pending_id).status
    assert final in {ActionStatus.APPROVED, ActionStatus.REJECTED}

    decisions = [
        entry
        for entry in service.entries_for(pending_id)
        if entry.action in {AuditAction.APPROVED, AuditAction.REJECTED}
    ]
    assert len(decisions) == 1
    assert decisions[0].new_status == final.value

    outcome = await confirm_once(review_settings, pending_id)
    if final is ActionStatus.APPROVED:
        assert outcome.payload["applied"] is True
        assert payments(engine) == 1
    else:
        assert outcome.payload["error_code"] == "review_rejected"
        assert payments(engine) == 0
    assert unbalanced(engine) == []
