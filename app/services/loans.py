"""Loan creation and reads."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ledger
from ..errors import LoanNotFound
from ..models import EntryDirection, Installment, LedgerAccount, Loan, LoanStatus
from ..schedule import Cadence, build_schedule


@dataclass(frozen=True)
class LoanView:
    loan: Loan
    installments: list[Installment]


def create_loan(
    session: Session,
    *,
    principal_cents: int,
    apr_bps: int,
    installment_count: int,
    cadence: Cadence,
    start_date: date,
) -> LoanView:
    """Create a loan with its schedule and the disbursement ledger transaction.

    The disbursement is a single balanced transaction: the receivable we now
    hold is debited for the whole amount payable, and that is matched by the
    principal we owe the merchant plus the interest we have earned the right to
    collect.
    """
    schedule = build_schedule(
        principal_cents=principal_cents,
        apr_bps=apr_bps,
        installment_count=installment_count,
        cadence=cadence,
        start_date=start_date,
    )

    loan = Loan(
        status=LoanStatus.OPEN,
        principal_cents=schedule.principal_cents,
        apr_bps=apr_bps,
        installment_count=installment_count,
        cadence=cadence,
        start_date=start_date,
        term_days=schedule.term_days,
        interest_cents=schedule.interest_cents,
        total_cents=schedule.total_cents,
    )
    session.add(loan)
    session.flush()

    installments = [
        Installment(
            loan_id=loan.id,
            sequence=scheduled.sequence,
            due_date=scheduled.due_date,
            amount_cents=scheduled.amount_cents,
            paid_cents=0,
        )
        for scheduled in schedule.installments
    ]
    session.add_all(installments)

    postings = [
        ledger.Posting(LedgerAccount.LOAN_RECEIVABLE, EntryDirection.DEBIT, schedule.total_cents),
        ledger.Posting(
            LedgerAccount.FUNDING_PAYABLE, EntryDirection.CREDIT, schedule.principal_cents
        ),
    ]
    if schedule.interest_cents > 0:
        postings.append(
            ledger.Posting(
                LedgerAccount.INTEREST_INCOME, EntryDirection.CREDIT, schedule.interest_cents
            )
        )
    ledger.post(session, loan_id=loan.id, postings=postings)

    session.flush()
    session.refresh(loan)
    return LoanView(loan=loan, installments=installments)


def load_loan(session: Session, loan_id: uuid.UUID, *, for_update: bool = False) -> LoanView:
    """Load a loan with its schedule, optionally taking the row lock."""
    statement = select(Loan).where(Loan.id == loan_id)
    if for_update:
        statement = statement.with_for_update()

    loan = session.execute(statement).scalar_one_or_none()
    if loan is None:
        raise LoanNotFound(f"No loan with id {loan_id}.", loan_id=str(loan_id))

    installments = list(
        session.execute(
            select(Installment).where(Installment.loan_id == loan_id).order_by(Installment.sequence)
        ).scalars()
    )
    return LoanView(loan=loan, installments=installments)
