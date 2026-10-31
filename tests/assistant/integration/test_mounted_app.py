"""The ledger's own endpoints, served through the mounted app.

Real Postgres, real HTTP, no LLM involved on the ledger paths. This is the test
that the outer application did not break the thing it wraps.
"""

from __future__ import annotations

import uuid

import httpx

LOAN_BODY = {
    "principal_cents": 120_000,
    "apr_bps": 0,
    "installment_count": 4,
    "cadence": "biweekly",
    "start_date": "2026-01-15",
}


def test_health_answers_through_the_mounted_app(mounted_client: httpx.Client) -> None:
    response = mounted_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "reachable"}


def test_the_assistant_has_its_own_health_endpoint(mounted_client: httpx.Client) -> None:
    response = mounted_client.get("/assistant/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["llm_provider"] == "fake"


def test_loans_can_be_created_and_read_through_the_mounted_app(
    mounted_client: httpx.Client,
) -> None:
    created = mounted_client.post(
        "/loans", json=LOAN_BODY, headers={"Idempotency-Key": f"mounted-{uuid.uuid4()}"}
    )
    assert created.status_code == 201
    loan = created.json()
    assert loan["outstanding_cents"] == 120_000
    assert sum(item["amount_cents"] for item in loan["installments"]) == loan["total_cents"]

    fetched = mounted_client.get(f"/loans/{loan['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == loan


def test_payments_and_the_ledger_work_through_the_mounted_app(
    mounted_client: httpx.Client,
) -> None:
    loan = mounted_client.post(
        "/loans", json=LOAN_BODY, headers={"Idempotency-Key": f"mounted-{uuid.uuid4()}"}
    ).json()

    paid = mounted_client.post(
        f"/loans/{loan['id']}/payments",
        json={"amount_cents": 30_000},
        headers={"Idempotency-Key": f"mounted-pay-{uuid.uuid4()}"},
    )
    assert paid.status_code == 201

    ledger = mounted_client.get(f"/loans/{loan['id']}/ledger")
    assert ledger.status_code == 200
    assert ledger.json()["net_cents"] == 0


def test_the_ledger_error_envelope_survives_the_mount(mounted_client: httpx.Client) -> None:
    missing = mounted_client.get(f"/loans/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "loan_not_found"

    no_key = mounted_client.post("/loans", json=LOAN_BODY)
    assert no_key.status_code == 400
    assert no_key.json()["error"]["code"] == "missing_idempotency_key"


def test_the_assistant_uses_the_same_error_envelope(mounted_client: httpx.Client) -> None:
    response = mounted_client.post("/assistant/chat", json={"session_id": "s", "unknown": 1})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_the_ledgers_own_openapi_document_is_untouched(mounted_client: httpx.Client) -> None:
    """The outer app serves no document, so these paths reach the ledger app."""
    document = mounted_client.get("/openapi.json").json()
    assert document["info"]["title"] == "installment-ledger"
    assert "/loans" in document["paths"]
    assert "/loans/{loan_id}/payments" in document["paths"]
    assert not any(path.startswith("/assistant") for path in document["paths"])
    assert mounted_client.get("/docs").status_code == 200


def test_the_assistant_has_its_own_openapi_document(mounted_client: httpx.Client) -> None:
    document = mounted_client.get("/assistant/openapi.json").json()
    assert document["info"]["title"] == "installment-ledger-assistant"
    assert "/chat" in document["paths"]
