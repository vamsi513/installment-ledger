"""Idempotent POST behaviour."""

from __future__ import annotations

import httpx
from sqlalchemy import Engine, text

from tests.helpers import DEFAULT_LOAN, create_loan, error_code, new_key, pay


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def test_replaying_a_loan_creation_returns_the_original_response(
    client: httpx.Client, engine: Engine
) -> None:
    key = new_key()
    first = client.post("/loans", json=DEFAULT_LOAN, headers={"Idempotency-Key": key})
    second = client.post("/loans", json=DEFAULT_LOAN, headers={"Idempotency-Key": key})

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    # Identical, not merely equivalent: the stored body is returned verbatim.
    assert second.text == first.text

    assert _count(engine, "loans") == 1
    assert _count(engine, "installments") == DEFAULT_LOAN["installment_count"]
    assert _count(engine, "idempotency_keys") == 1
    assert _count(engine, "ledger_entries") == 2


def test_replaying_a_payment_returns_the_original_response(
    client: httpx.Client, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    key = new_key()

    first = pay(client, loan["id"], 30_000, key=key)
    second = pay(client, loan["id"], 30_000, key=key)

    assert first.status_code == second.status_code == 201
    assert second.text == first.text

    assert _count(engine, "payments") == 1
    assert _count(engine, "payment_allocations") == 1
    assert client.get(f"/loans/{loan['id']}").json()["outstanding_cents"] == 90_000


def test_same_key_with_a_different_body_is_rejected(client: httpx.Client, engine: Engine) -> None:
    key = new_key()
    first = client.post("/loans", json=DEFAULT_LOAN, headers={"Idempotency-Key": key})
    assert first.status_code == 201

    changed = DEFAULT_LOAN | {"principal_cents": 999_999}
    second = client.post("/loans", json=changed, headers={"Idempotency-Key": key})

    assert second.status_code == 409
    assert error_code(second) == "idempotency_key_conflict"
    assert second.json()["error"]["details"]["idempotency_key"] == key
    assert _count(engine, "loans") == 1


def test_same_payment_key_with_a_different_amount_is_rejected(
    client: httpx.Client, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    key = new_key()

    assert pay(client, loan["id"], 10_000, key=key).status_code == 201
    second = pay(client, loan["id"], 20_000, key=key)

    assert second.status_code == 409
    assert error_code(second) == "idempotency_key_conflict"
    assert _count(engine, "payments") == 1
    assert client.get(f"/loans/{loan['id']}").json()["outstanding_cents"] == 110_000


def test_reusing_a_key_on_a_different_endpoint_is_rejected(client: httpx.Client) -> None:
    key = new_key()
    loan = client.post("/loans", json=DEFAULT_LOAN, headers={"Idempotency-Key": key})
    assert loan.status_code == 201

    reused = pay(client, loan.json()["id"], 1_000, key=key)
    assert reused.status_code == 409
    assert error_code(reused) == "idempotency_key_conflict"


def test_reusing_a_key_on_a_different_loan_is_rejected(client: httpx.Client) -> None:
    first_loan = create_loan(client)
    second_loan = create_loan(client)
    key = new_key()

    assert pay(client, first_loan["id"], 1_000, key=key).status_code == 201
    crossed = pay(client, second_loan["id"], 1_000, key=key)

    assert crossed.status_code == 409
    assert error_code(crossed) == "idempotency_key_conflict"


def test_a_rejected_request_does_not_burn_its_key(client: httpx.Client, engine: Engine) -> None:
    """A failed attempt rolls its key row back, so the caller can retry."""
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    key = new_key()

    rejected = pay(client, loan["id"], 999_999, key=key)
    assert rejected.status_code == 422
    assert error_code(rejected) == "overpayment"
    assert _count(engine, "idempotency_keys") == 1  # only the loan creation key

    retried = pay(client, loan["id"], 30_000, key=key)
    assert retried.status_code == 201
    assert _count(engine, "payments") == 1


def test_a_missing_idempotency_key_is_rejected(client: httpx.Client, engine: Engine) -> None:
    response = client.post("/loans", json=DEFAULT_LOAN)
    assert response.status_code == 400
    assert error_code(response) == "missing_idempotency_key"
    assert _count(engine, "loans") == 0


def test_a_blank_idempotency_key_is_rejected(client: httpx.Client, engine: Engine) -> None:
    response = client.post("/loans", json=DEFAULT_LOAN, headers={"Idempotency-Key": ""})
    assert response.status_code == 400
    assert error_code(response) == "missing_idempotency_key"
    assert _count(engine, "loans") == 0


def test_key_reuse_is_detected_regardless_of_json_key_order(client: httpx.Client) -> None:
    """The request hash is over the validated body, not the raw bytes."""
    key = new_key()
    ordered = dict(sorted(DEFAULT_LOAN.items()))
    reversed_order = dict(reversed(list(DEFAULT_LOAN.items())))

    first = client.post("/loans", json=ordered, headers={"Idempotency-Key": key})
    second = client.post("/loans", json=reversed_order, headers={"Idempotency-Key": key})

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.text == first.text
