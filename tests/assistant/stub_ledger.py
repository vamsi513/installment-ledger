"""A stand in for the ledger, used to produce failures the real ledger will not.

A timeout, a 5xx and a 409 on the payments endpoint are all things the agent has
to handle and none of them can be provoked reliably from the real service. This
app serves a fixed loan body and misbehaves on demand for the write.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse

PaymentBehaviour = Literal["ok", "timeout", "server_error", "conflict"]


def fixed_loan(loan_id: str, *, outstanding: int = 120_000) -> dict[str, Any]:
    """A loan body shaped exactly like the real presenter's output."""
    per_installment = outstanding // 4
    return {
        "id": loan_id,
        "status": "open",
        "principal_cents": outstanding,
        "apr_bps": 0,
        "installment_count": 4,
        "cadence": "biweekly",
        "start_date": "2026-01-15",
        "term_days": 56,
        "interest_cents": 0,
        "total_cents": outstanding,
        "paid_cents": 0,
        "outstanding_cents": outstanding,
        "created_at": "2026-01-15T00:00:00+00:00",
        "installments": [
            {
                "id": f"0000000{index}-1111-4111-8111-111111111111",
                "sequence": index + 1,
                "due_date": f"2026-0{index + 1}-29",
                "amount_cents": per_installment,
                "paid_cents": 0,
                "outstanding_cents": per_installment,
            }
            for index in range(4)
        ],
    }


def build_stub_ledger(
    *,
    loan: dict[str, Any],
    payment_behaviour: PaymentBehaviour = "ok",
    delay_seconds: float = 30.0,
) -> FastAPI:
    application = FastAPI()

    @application.get("/loans/{loan_id}")
    async def get_loan(loan_id: str) -> JSONResponse:
        return JSONResponse(status_code=200, content=loan)

    @application.post("/loans/{loan_id}/payments")
    async def post_payment(loan_id: str) -> JSONResponse:
        if payment_behaviour == "timeout":
            await asyncio.sleep(delay_seconds)
        if payment_behaviour == "server_error":
            return JSONResponse(status_code=503, content={"detail": "unavailable"})
        if payment_behaviour == "conflict":
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "idempotency_key_conflict",
                        "message": (
                            "This Idempotency-Key was already used with a different request body."
                        ),
                        "details": {},
                    }
                },
            )
        return JSONResponse(
            status_code=201,
            content={
                "id": "aaaaaaaa-1111-4111-8111-111111111111",
                "loan_id": loan["id"],
                "amount_cents": 1_000,
                "transaction_id": "bbbbbbbb-1111-4111-8111-111111111111",
                "created_at": "2026-01-15T00:00:00+00:00",
                "allocations": [],
                "loan": {**loan, "paid_cents": 1_000, "outstanding_cents": 119_000},
            },
        )

    return application
