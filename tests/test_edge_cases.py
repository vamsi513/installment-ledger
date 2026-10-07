"""Rejections and boundaries."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import Engine, text

from tests.helpers import DEFAULT_LOAN, create_loan, error_code, new_key, pay


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


@pytest.mark.parametrize(
    "override",
    [
        {"principal_cents": 0},
        {"principal_cents": -1},
        {"principal_cents": -100_000},
        {"principal_cents": 99},
        {"principal_cents": 100_000_001},
        {"principal_cents": 1.5},
        {"principal_cents": "100000"},
        {"principal_cents": 100000.0},
        {"principal_cents": True},
        {"apr_bps": -1},
        {"apr_bps": 5_001},
        {"apr_bps": 0.5},
        {"installment_count": 0},
        {"installment_count": 1},
        {"installment_count": -4},
        {"installment_count": 61},
        {"cadence": "weekly"},
        {"cadence": ""},
        {"start_date": "not-a-date"},
        {"start_date": "2026-02-30"},
    ],
)
def test_invalid_loan_requests_are_rejected(
    client: httpx.Client, engine: Engine, override: dict
) -> None:
    response = client.post(
        "/loans", json=DEFAULT_LOAN | override, headers={"Idempotency-Key": new_key()}
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    assert response.json()["error"]["details"]["fields"]
    assert _count(engine, "loans") == 0


@pytest.mark.parametrize("missing", sorted(DEFAULT_LOAN))
def test_a_loan_request_missing_a_field_is_rejected(
    client: httpx.Client, engine: Engine, missing: str
) -> None:
    body = {key: value for key, value in DEFAULT_LOAN.items() if key != missing}
    response = client.post("/loans", json=body, headers={"Idempotency-Key": new_key()})
    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert _count(engine, "loans") == 0


def test_an_unknown_field_is_rejected(client: httpx.Client, engine: Engine) -> None:
    response = client.post(
        "/loans",
        json=DEFAULT_LOAN | {"late_fee_cents": 500},
        headers={"Idempotency-Key": new_key()},
    )
    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert _count(engine, "loans") == 0


@pytest.mark.parametrize("amount", [0, -1, -5_000, 0.5, 100.0, True, "100"])
def test_invalid_payment_amounts_are_rejected(
    client: httpx.Client, engine: Engine, amount: object
) -> None:
    loan = create_loan(client)
    response = client.post(
        f"/loans/{loan['id']}/payments",
        json={"amount_cents": amount},
        headers={"Idempotency-Key": new_key()},
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    assert _count(engine, "payments") == 0


def test_overpayment_is_rejected_and_reports_the_outstanding_balance(
    client: httpx.Client, engine: Engine
) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)

    response = pay(client, loan["id"], 120_001)
    assert response.status_code == 422
    assert error_code(response) == "overpayment"
    assert response.json()["error"]["details"] == {
        "amount_cents": 120_001,
        "outstanding_cents": 120_000,
    }
    assert _count(engine, "payments") == 0
    assert _count(engine, "ledger_entries") == 2  # only the disbursement


def test_overpayment_is_measured_against_the_remaining_balance(client: httpx.Client) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    assert pay(client, loan["id"], 100_000).status_code == 201

    response = pay(client, loan["id"], 20_001)
    assert response.status_code == 422
    assert response.json()["error"]["details"]["outstanding_cents"] == 20_000

    assert pay(client, loan["id"], 20_000).status_code == 201


def test_paying_a_closed_loan_is_rejected(client: httpx.Client, engine: Engine) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    assert pay(client, loan["id"], 120_000).status_code == 201

    response = pay(client, loan["id"], 1)
    assert response.status_code == 422
    assert error_code(response) == "loan_closed"
    assert _count(engine, "payments") == 1


def test_an_unknown_loan_id_is_a_not_found(client: httpx.Client) -> None:
    unknown = uuid.uuid4()

    fetched = client.get(f"/loans/{unknown}")
    assert fetched.status_code == 404
    assert error_code(fetched) == "loan_not_found"

    ledger = client.get(f"/loans/{unknown}/ledger")
    assert ledger.status_code == 404
    assert error_code(ledger) == "loan_not_found"

    payment = pay(client, str(unknown), 1_000)
    assert payment.status_code == 404
    assert error_code(payment) == "loan_not_found"


def test_a_malformed_loan_id_is_a_validation_error(client: httpx.Client) -> None:
    response = client.get("/loans/not-a-uuid")
    assert response.status_code == 422
    assert error_code(response) == "validation_error"


def test_the_smallest_allowed_loan_still_produces_whole_cent_installments(
    client: httpx.Client,
) -> None:
    loan = create_loan(client, principal_cents=100, installment_count=60, cadence="monthly")
    amounts = [item["amount_cents"] for item in loan["installments"]]

    assert sum(amounts) == 100
    assert min(amounts) >= 1
    assert amounts[:40] == [2] * 40
    assert amounts[40:] == [1] * 20


def test_a_loan_can_be_paid_off_one_cent_at_a_time(client: httpx.Client) -> None:
    loan = create_loan(client, principal_cents=100, installment_count=2)
    loan_id = loan["id"]

    for _ in range(100):
        assert pay(client, loan_id, 1).status_code == 201

    final = client.get(f"/loans/{loan_id}").json()
    assert final["status"] == "closed"
    assert final["outstanding_cents"] == 0

    ledger = client.get(f"/loans/{loan_id}/ledger").json()
    assert ledger["net_cents"] == 0
    assert len(ledger["transactions"]) == 101
