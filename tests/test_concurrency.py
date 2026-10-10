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


# A heavier mix than the tests above: many payments at once, where a third of the
# threads deliberately reuse one idempotency key. The point is to assert every
# invariant at once afterwards rather than one per test, because the failure mode
# worth catching is a combination that each individual check would miss.
HEAVY_THREAD_COUNT = 24


def test_a_storm_of_payments_and_duplicate_keys_keeps_every_invariant(
    client: httpx.Client, base_url: str, engine: Engine
) -> None:
    principal = 240_000
    loan = create_loan(client, principal_cents=principal, installment_count=6)
    loan_id = loan["id"]
    total_payable = loan["total_cents"]

    # Eight threads share one key, so at most one of their payments may land. The
    # other sixteen are distinct and should all land.
    shared_key = new_key()
    unique_threads = 16
    amount = 1_000

    def send(http: httpx.Client, index: int) -> httpx.Response:
        key = shared_key if index >= unique_threads else new_key()
        return http.post(
            f"/loans/{loan_id}/payments",
            json={"amount_cents": amount},
            headers={"Idempotency-Key": key},
        )

    responses = _run_in_parallel(base_url, send, count=HEAVY_THREAD_COUNT)
    accepted = [r for r in responses if r.status_code in (200, 201)]
    assert len(accepted) == HEAVY_THREAD_COUNT, [r.status_code for r in responses]

    # The shared key must have produced exactly one payment row, however many
    # threads used it.
    with engine.connect() as connection:
        payment_rows = int(connection.execute(text("SELECT COUNT(*) FROM payments")).scalar_one())
    expected_payments = unique_threads + 1
    assert payment_rows == expected_payments, "a duplicate key created an extra payment"

    state = client.get(f"/loans/{loan_id}").json()

    # 1. Money applied matches exactly the payments that were allowed to exist.
    expected_paid = expected_payments * amount
    assert state["paid_cents"] == expected_paid

    # 2. Outstanding never goes negative and always complements what was paid.
    assert state["outstanding_cents"] >= 0
    assert state["outstanding_cents"] == total_payable - expected_paid

    # 3. Installments still sum to the total payable, unchanged by any payment.
    assert sum(i["amount_cents"] for i in state["installments"]) == total_payable

    # 4. No installment is over applied or negative.
    for installment in state["installments"]:
        assert 0 <= installment["paid_cents"] <= installment["amount_cents"]

    # 5. Applied amounts across installments equal the money taken in.
    assert sum(i["paid_cents"] for i in state["installments"]) == expected_paid

    # 6. The double entry ledger still balances. Entries carry a direction and a
    # single amount, so debits and credits are summed conditionally.
    with engine.connect() as connection:
        debits, credits = connection.execute(
            text(
                "SELECT "
                "COALESCE(SUM(amount_cents) FILTER (WHERE direction = 'debit'), 0), "
                "COALESCE(SUM(amount_cents) FILTER (WHERE direction = 'credit'), 0) "
                "FROM ledger_entries"
            )
        ).one()
    assert int(debits) == int(credits), "the ledger stopped balancing under contention"
    assert int(debits) > 0, "no ledger entries were written at all"


def test_the_same_key_under_contention_returns_one_payment_id(
    client: httpx.Client, base_url: str, engine: Engine
) -> None:
    """Every caller that reuses a key must be told about the same payment, not just
    be refused. A retry that gets a different id would let a caller double count."""
    loan = create_loan(client, principal_cents=50_000, installment_count=2)
    loan_id = loan["id"]
    key = new_key()

    def send(http: httpx.Client, _index: int) -> httpx.Response:
        return http.post(
            f"/loans/{loan_id}/payments",
            json={"amount_cents": 2_500},
            headers={"Idempotency-Key": key},
        )

    responses = _run_in_parallel(base_url, send, count=HEAVY_THREAD_COUNT)
    successes = [r for r in responses if r.status_code in (200, 201)]
    conflicts = [r for r in responses if r.status_code not in (200, 201)]

    assert successes, "no caller got a usable answer"
    ids = {r.json()["id"] for r in successes}
    assert len(ids) == 1, f"one key produced {len(ids)} payment ids"

    # Anything not served the stored result must be a documented conflict, never a
    # 500. A crash here would be the thing that embarrasses a demo.
    for response in conflicts:
        assert response.status_code < 500, response.text
        assert error_code(response)

    assert _count(engine, "payments") == 1
