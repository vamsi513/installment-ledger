"""Shared helpers for the HTTP tests."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

DEFAULT_LOAN = {
    "principal_cents": 120_000,
    "apr_bps": 0,
    "installment_count": 4,
    "cadence": "biweekly",
    "start_date": "2026-01-15",
}


def new_key() -> str:
    return f"key-{uuid.uuid4()}"


def create_loan(client: httpx.Client, **overrides: Any) -> dict[str, Any]:
    body = DEFAULT_LOAN | overrides
    response = client.post("/loans", json=body, headers={"Idempotency-Key": new_key()})
    assert response.status_code == 201, response.text
    return response.json()


def pay(
    client: httpx.Client, loan_id: str, amount_cents: int, key: str | None = None
) -> httpx.Response:
    return client.post(
        f"/loans/{loan_id}/payments",
        json={"amount_cents": amount_cents},
        headers={"Idempotency-Key": key or new_key()},
    )


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]
