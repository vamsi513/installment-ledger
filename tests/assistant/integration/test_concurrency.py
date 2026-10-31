"""The same confirmation fired from several real threads.

Each thread gets its own event loop, its own MCP subprocess and its own
connection to the ledger, and they are lined up on a barrier so they contend on
purpose. The invariant is the one that matters: one payment row, whatever the
interleaving.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import Engine, text

from assistant.agent import run_turn
from assistant.agent.state import Outcome
from assistant.config import Settings
from assistant.mcp_client import ToolCallResult, ToolGateway
from tests.helpers import create_loan

THREAD_COUNT = 6


def count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def unbalanced_transactions(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT transaction_id
                  FROM ledger_entries
                 GROUP BY transaction_id
                HAVING SUM(CASE WHEN direction = 'debit'
                                THEN amount_cents ELSE -amount_cents END) <> 0
                """
            )
        ).all()
    return [str(row[0]) for row in rows]


@pytest.mark.anyio
async def test_one_prepared_payment_confirmed_from_many_threads_pays_once(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)

    async with ToolGateway(assistant_settings) as tools:
        prepared = await tools.call(
            "prepare_payment", {"loan_id": loan["id"], "amount_cents": 45_000}
        )
    pending_id = prepared.payload["pending_action_id"]

    barrier = threading.Barrier(THREAD_COUNT)

    def confirm_from_thread(_: int) -> ToolCallResult:
        async def once() -> ToolCallResult:
            async with ToolGateway(assistant_settings) as tools:
                barrier.wait(timeout=60)
                return await tools.call("confirm_payment", {"pending_action_id": pending_id})

        return asyncio.run(once())

    with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
        results = list(pool.map(confirm_from_thread, range(THREAD_COUNT)))

    assert [result.status for result in results] == ["ok"] * THREAD_COUNT
    assert all(result.payload["applied"] is True for result in results)
    # Every caller was handed the same payment, because they all sent the same
    # idempotency key.
    assert len({result.payload["payment_id"] for result in results}) == 1

    assert count(engine, "payments") == 1
    final = client.get(f"/loans/{loan['id']}").json()
    assert final["paid_cents"] == 45_000
    assert final["outstanding_cents"] == 75_000
    assert unbalanced_transactions(engine) == []
    assert client.get(f"/loans/{loan['id']}/ledger").json()["net_cents"] == 0


@pytest.mark.anyio
async def test_separate_sessions_confirming_at_once_each_pay_once(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """Different prepared payments on one loan, confirmed simultaneously."""
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    share = 10_000

    sessions = [f"c-{uuid.uuid4().hex[:8]}" for _ in range(THREAD_COUNT)]
    for session in sessions:
        prepared = await run_turn(
            session_id=session,
            message=f"pay {share} cents",
            bound_loan_ids=[loan["id"]],
            settings=assistant_settings,
        )
        assert prepared.outcome == Outcome.PREPARED.value

    barrier = threading.Barrier(THREAD_COUNT)

    def confirm(session: str):
        async def once():
            barrier.wait(timeout=60)
            return await run_turn(
                session_id=session,
                message="yes",
                bound_loan_ids=[loan["id"]],
                settings=assistant_settings,
            )

        return asyncio.run(once())

    with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
        results = list(pool.map(confirm, sessions))

    assert [result.outcome for result in results] == [Outcome.APPLIED.value] * THREAD_COUNT
    assert count(engine, "payments") == THREAD_COUNT

    final = client.get(f"/loans/{loan['id']}").json()
    assert final["paid_cents"] == share * THREAD_COUNT
    assert unbalanced_transactions(engine) == []


@pytest.mark.anyio
async def test_simultaneous_confirmations_cannot_overpay(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """Five prepared payments, only three of which fit inside the balance."""
    loan = create_loan(client, principal_cents=30_000, installment_count=4)
    attempts = 5
    share = 10_000

    pending_ids: list[str] = []
    async with ToolGateway(assistant_settings) as tools:
        for _ in range(attempts):
            prepared = await tools.call(
                "prepare_payment", {"loan_id": loan["id"], "amount_cents": share}
            )
            assert prepared.status == "ok"
            pending_ids.append(prepared.payload["pending_action_id"])

    barrier = threading.Barrier(attempts)

    def confirm(pending_id: str) -> ToolCallResult:
        async def once() -> ToolCallResult:
            async with ToolGateway(assistant_settings) as tools:
                barrier.wait(timeout=60)
                return await tools.call("confirm_payment", {"pending_action_id": pending_id})

        return asyncio.run(once())

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        results = list(pool.map(confirm, pending_ids))

    applied = [result for result in results if result.status == "ok"]
    refused = [result for result in results if result.status == "rejected"]

    assert len(applied) == 3
    assert len(refused) == attempts - 3
    # Whichever confirmation arrives after the balance is gone is refused, and
    # the last one to arrive finds the loan closed rather than merely overpaid.
    assert {result.payload["error_code"] for result in refused} <= {
        "overpayment",
        "loan_closed",
    }
    assert all(result.payload["applied"] is False for result in refused)

    final = client.get(f"/loans/{loan['id']}").json()
    assert final["paid_cents"] == 30_000
    assert final["outstanding_cents"] == 0
    assert final["status"] == "closed"
    assert count(engine, "payments") == 3
    assert unbalanced_transactions(engine) == []
