"""HTTP routes.

Handlers are ordinary synchronous functions. Starlette runs them in its thread
pool, so concurrent requests really do hit Postgres from separate connections
at the same time, which is the behaviour the concurrency tests exercise.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import presenters
from ..db import new_session
from ..errors import MissingIdempotencyKey
from ..idempotency import run_once
from ..models import LedgerEntry
from ..schemas import CreateLoanRequest, CreatePaymentRequest
from ..services.loans import create_loan, load_loan
from ..services.payments import apply_payment

router = APIRouter()

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


def _require_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise MissingIdempotencyKey("An Idempotency-Key header is required on this endpoint.")
    return value.strip()


@router.get("/health")
def health() -> Response:
    """Report whether the service can reach its database."""
    with new_session() as session:
        session.execute(text("SELECT 1"))
    return JSONResponse(status_code=200, content={"status": "ok", "database": "reachable"})


@router.post("/loans")
def post_loan(request: CreateLoanRequest, idempotency_key: IdempotencyKey = None) -> Response:
    key = _require_key(idempotency_key)
    payload = request.model_dump(mode="json")

    def work(session: Session) -> tuple[int, dict]:
        view = create_loan(
            session,
            principal_cents=request.principal_cents,
            apr_bps=request.apr_bps,
            installment_count=request.installment_count,
            cadence=request.cadence,
            start_date=request.start_date,
        )
        return 201, presenters.loan_body(view)

    status, body = run_once(key=key, endpoint="POST /loans", payload=payload, work=work)
    return JSONResponse(status_code=status, content=body)


@router.get("/loans/{loan_id}")
def get_loan(loan_id: uuid.UUID) -> Response:
    with new_session() as session:
        view = load_loan(session, loan_id)
        return JSONResponse(status_code=200, content=presenters.loan_body(view))


@router.post("/loans/{loan_id}/payments")
def post_payment(
    loan_id: uuid.UUID,
    request: CreatePaymentRequest,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    key = _require_key(idempotency_key)
    payload = request.model_dump(mode="json")

    def work(session: Session) -> tuple[int, dict]:
        result = apply_payment(session, loan_id=loan_id, amount_cents=request.amount_cents)
        return 201, presenters.payment_body(result)

    status, body = run_once(
        key=key, endpoint=f"POST /loans/{loan_id}/payments", payload=payload, work=work
    )
    return JSONResponse(status_code=status, content=body)


@router.get("/loans/{loan_id}/ledger")
def get_ledger(loan_id: uuid.UUID) -> Response:
    with new_session() as session:
        load_loan(session, loan_id)
        entries = list(
            session.execute(
                select(LedgerEntry)
                .where(LedgerEntry.loan_id == loan_id)
                .order_by(LedgerEntry.created_at, LedgerEntry.id)
            ).scalars()
        )
        return JSONResponse(status_code=200, content=presenters.ledger_body(str(loan_id), entries))
