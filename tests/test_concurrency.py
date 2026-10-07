"""Concurrency, with real OS threads against real Postgres.

Each thread uses its own HTTP connection, so the requests arrive at the server
in parallel and are handled on separate database connections. A barrier lines
the threads up so they contend on purpose rather than by luck.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from sqlalchemy import Engine, text

from tests.helpers import DEFAULT_LOAN, create_loan, error_code, new_key

THREAD_COUNT = 8


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def _run_in_parallel(base_url: str, send: Any, count: int = THREAD_COUNT) -> list[httpx.Response]:
    """Fire ``count`` requests that all cross the barrier together."""
    barrier = threading.Barrier(count)

    def task(index: int) -> httpx.Response:
        with httpx.Client(base_url=base_url, timeout=60.0) as client:
            barrier.wait(timeout=30)
            return send(client, index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(task, range(count)))


def test_simultaneous_payments_do_not_overpay_or_double_apply(
    client: httpx.Client, base_url: str, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    loan_id = loan["id"]
    share = 120_000 // THREAD_COUNT

    def send(http: httpx.Client, _: int) -> httpx.Response:
        return http.post(
            f"/loans/{loan_id}/payments",
            json={"amount_cents": share},
            headers={"Idempotency-Key": new_key()},
        )

    responses = _run_in_parallel(base_url, send)

    assert [response.status_code for response in responses] == [201] * THREAD_COUNT
    assert _count(engine, "payments") == THREAD_COUNT

    final = client.get(f"/loans/{loan_id}").json()
    assert final["paid_cents"] == 120_000
    assert final["outstanding_cents"] == 0
    assert final["status"] == "closed"
    assert [item["outstanding_cents"] for item in final["installments"]] == [0, 0, 0, 0]


def test_simultaneous_payments_past_the_balance_are_partly_rejected(
    client: httpx.Client, base_url: str, engine: Engine
) -> None:
    """Only as many payments as the balance allows may succeed."""
    loan = create_loan(client, principal_cents=100_000, installment_count=4)
    loan_id = loan["id"]
    amount = 30_000  # three fit inside the balance, the rest must be refused

    def send(http: httpx.Client, _: int) -> httpx.Response:
        return http.post(
            f"/loans/{loan_id}/payments",
            json={"amount_cents": amount},
            headers={"Idempotency-Key": new_key()},
        )

    responses = _run_in_parallel(base_url, send)
    statuses = sorted(response.status_code for response in responses)

    assert statuses == [201, 201, 201] + [422] * (THREAD_COUNT - 3)
    assert all(
        error_code(response) == "overpayment"
        for response in responses
        if response.status_code == 422
    )
    assert _count(engine, "payments") == 3

    final = client.get(f"/loans/{loan_id}").json()
    assert final["paid_cents"] == 90_000
    assert final["outstanding_cents"] == 10_000
    assert final["status"] == "open"


def test_simultaneous_payments_with_one_key_produce_exactly_one_payment(
    client: httpx.Client, base_url: str, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    loan_id = loan["id"]
    shared_key = new_key()

    def send(http: httpx.Client, _: int) -> httpx.Response:
        return http.post(
            f"/loans/{loan_id}/payments",
            json={"amount_cents": 30_000},
            headers={"Idempotency-Key": shared_key},
        )

    responses = _run_in_parallel(base_url, send)

    assert [response.status_code for response in responses] == [201] * THREAD_COUNT
    bodies = {response.text for response in responses}
    assert len(bodies) == 1  # every caller got the same response

    assert _count(engine, "payments") == 1
    assert _count(engine, "payment_allocations") == 1

    final = client.get(f"/loans/{loan_id}").json()
    assert final["paid_cents"] == 30_000
    assert final["outstanding_cents"] == 90_000


def test_simultaneous_loan_creations_with_one_key_produce_exactly_one_loan(
    base_url: str, engine: Engine
) -> None:
    shared_key = new_key()

    def send(http: httpx.Client, _: int) -> httpx.Response:
        return http.post("/loans", json=DEFAULT_LOAN, headers={"Idempotency-Key": shared_key})

    responses = _run_in_parallel(base_url, send)

    assert [response.status_code for response in responses] == [201] * THREAD_COUNT
    assert len({response.text for response in responses}) == 1

    assert _count(engine, "loans") == 1
    assert _count(engine, "idempotency_keys") == 1
    assert _count(engine, "installments") == DEFAULT_LOAN["installment_count"]
    assert _count(engine, "ledger_entries") == 2


def test_the_ledger_still_balances_after_concurrent_payments(
    client: httpx.Client, base_url: str, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=100_003, apr_bps=1_250, installment_count=6)
    loan_id = loan["id"]
    total = loan["total_cents"]
    share = total // THREAD_COUNT

    def send(http: httpx.Client, _: int) -> httpx.Response:
        return http.post(
            f"/loans/{loan_id}/payments",
            json={"amount_cents": share},
            headers={"Idempotency-Key": new_key()},
        )

    responses = _run_in_parallel(base_url, send)
    assert [response.status_code for response in responses] == [201] * THREAD_COUNT

    with engine.connect() as connection:
        unbalanced = connection.execute(
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
    assert unbalanced == []

    ledger = client.get(f"/loans/{loan_id}/ledger").json()
    assert ledger["net_cents"] == 0
    assert len(ledger["transactions"]) == THREAD_COUNT + 1

    final = client.get(f"/loans/{loan_id}").json()
    assert final["paid_cents"] == share * THREAD_COUNT
    assert final["outstanding_cents"] == total - share * THREAD_COUNT


def test_concurrent_payments_on_separate_loans_do_not_block_each_other(
    client: httpx.Client, base_url: str, engine: Engine
) -> None:
    loans = [create_loan(client, principal_cents=120_000, installment_count=4) for _ in range(4)]

    def send(http: httpx.Client, index: int) -> httpx.Response:
        loan_id = loans[index % len(loans)]["id"]
        return http.post(
            f"/loans/{loan_id}/payments",
            json={"amount_cents": 15_000},
            headers={"Idempotency-Key": new_key()},
        )

    responses = _run_in_parallel(base_url, send)
    assert [response.status_code for response in responses] == [201] * THREAD_COUNT
    assert _count(engine, "payments") == THREAD_COUNT

    for loan in loans:
        state = client.get(f"/loans/{loan['id']}").json()
        assert state["paid_cents"] == 30_000
        assert state["outstanding_cents"] == 90_000
