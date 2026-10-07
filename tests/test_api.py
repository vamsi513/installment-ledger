"""End to end behaviour of the endpoints."""

from __future__ import annotations

import httpx

from tests.helpers import create_loan, new_key, pay


def test_health_reports_the_database_is_reachable(client: httpx.Client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "reachable"}


def test_create_loan_returns_a_schedule_that_sums_to_the_total(client: httpx.Client) -> None:
    loan = create_loan(client, principal_cents=100_003, installment_count=4, apr_bps=0)

    assert loan["status"] == "open"
    assert loan["principal_cents"] == 100_003
    assert loan["interest_cents"] == 0
    assert loan["total_cents"] == 100_003
    assert loan["outstanding_cents"] == 100_003
    assert loan["paid_cents"] == 0

    amounts = [item["amount_cents"] for item in loan["installments"]]
    assert amounts == [25_001, 25_001, 25_001, 25_000]
    assert sum(amounts) == loan["total_cents"]
    assert [item["due_date"] for item in loan["installments"]] == [
        "2026-01-29",
        "2026-02-12",
        "2026-02-26",
        "2026-03-12",
    ]


def test_create_loan_with_an_apr_adds_simple_interest(client: httpx.Client) -> None:
    loan = create_loan(
        client,
        principal_cents=500_000,
        apr_bps=1_200,
        installment_count=6,
        cadence="monthly",
        start_date="2026-01-15",
    )
    assert loan["term_days"] == 181
    assert loan["interest_cents"] == 29_753
    assert loan["total_cents"] == 529_753
    assert sum(item["amount_cents"] for item in loan["installments"]) == 529_753


def test_get_loan_matches_what_create_returned(client: httpx.Client) -> None:
    created = create_loan(client)
    fetched = client.get(f"/loans/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_payments_fill_the_oldest_unpaid_installment_first(client: httpx.Client) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    loan_id = loan["id"]

    first = pay(client, loan_id, 45_000)
    assert first.status_code == 201
    body = first.json()

    assert body["amount_cents"] == 45_000
    assert body["allocations"] == [
        {
            "installment_sequence": 1,
            "installment_id": loan["installments"][0]["id"],
            "amount_cents": 30_000,
        },
        {
            "installment_sequence": 2,
            "installment_id": loan["installments"][1]["id"],
            "amount_cents": 15_000,
        },
    ]

    installments = body["loan"]["installments"]
    assert [item["paid_cents"] for item in installments] == [30_000, 15_000, 0, 0]
    assert body["loan"]["outstanding_cents"] == 75_000
    assert body["loan"]["status"] == "open"


def test_paying_the_full_balance_closes_the_loan(client: httpx.Client) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    loan_id = loan["id"]

    assert pay(client, loan_id, 60_000).status_code == 201
    final = pay(client, loan_id, 60_000)
    assert final.status_code == 201

    body = final.json()["loan"]
    assert body["status"] == "closed"
    assert body["outstanding_cents"] == 0
    assert body["paid_cents"] == 120_000
    assert all(item["outstanding_cents"] == 0 for item in body["installments"])


def test_ledger_endpoint_groups_entries_by_transaction(client: httpx.Client) -> None:
    loan = create_loan(client, principal_cents=100_000, apr_bps=1_000, installment_count=4)
    loan_id = loan["id"]
    pay(client, loan_id, 50_000)

    response = client.get(f"/loans/{loan_id}/ledger")
    assert response.status_code == 200
    body = response.json()

    assert body["loan_id"] == loan_id
    assert body["net_cents"] == 0
    assert body["debit_cents"] == body["credit_cents"]
    assert len(body["transactions"]) == 2
    assert all(transaction["net_cents"] == 0 for transaction in body["transactions"])

    disbursement, payment = body["transactions"]
    assert sorted(entry["account"] for entry in disbursement["entries"]) == [
        "funding_payable",
        "interest_income",
        "loan_receivable",
    ]
    assert sorted(entry["account"] for entry in payment["entries"]) == ["cash", "loan_receivable"]


def test_a_zero_apr_loan_posts_no_interest_entry(client: httpx.Client) -> None:
    loan = create_loan(client, apr_bps=0)
    body = client.get(f"/loans/{loan['id']}/ledger").json()

    entries = body["transactions"][0]["entries"]
    assert len(entries) == 2
    assert {entry["account"] for entry in entries} == {"loan_receivable", "funding_payable"}


def test_partial_payments_are_tracked_installment_by_installment(client: httpx.Client) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    loan_id = loan["id"]

    for amount in (10_000, 10_000, 10_000):
        assert pay(client, loan_id, amount).status_code == 201

    fetched = client.get(f"/loans/{loan_id}").json()
    assert [item["paid_cents"] for item in fetched["installments"]] == [30_000, 0, 0, 0]
    assert fetched["installments"][0]["outstanding_cents"] == 0
    assert fetched["outstanding_cents"] == 90_000


def test_each_loan_response_carries_its_own_idempotent_identity(client: httpx.Client) -> None:
    first = client.post(
        "/loans",
        json={
            "principal_cents": 100_000,
            "apr_bps": 0,
            "installment_count": 4,
            "cadence": "biweekly",
            "start_date": "2026-01-15",
        },
        headers={"Idempotency-Key": new_key()},
    )
    second = client.post(
        "/loans",
        json={
            "principal_cents": 100_000,
            "apr_bps": 0,
            "installment_count": 4,
            "cadence": "biweekly",
            "start_date": "2026-01-15",
        },
        headers={"Idempotency-Key": new_key()},
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
