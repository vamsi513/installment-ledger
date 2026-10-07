"""Payment application.

Two rules decide where money goes, and both are enforced while holding the
loan's row lock so concurrent payments cannot interleave:

* a payment is rejected outright if it exceeds the outstanding balance, and
* whatever is paid lands on the oldest unpaid installment first, carrying any
  remainder forward, so partial payments are tracked installment by
  installment.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .. import ledger
from ..errors import LoanClosed, Overpayment
from ..models import (
    EntryDirection,
    Installment,
    LedgerAccount,
    Loan,
    LoanStatus,
    Payment,
    PaymentAllocation,
)
from .loans import load_loan


@dataclass(frozen=True)
class Allocation:
    installment: Installment
    amount_cents: int


@dataclass(frozen=True)
class PaymentResult:
    payment: Payment
    allocations: list[Allocation]
    loan: Loan
    installments: list[Installment]


def outstanding_cents(installments: list[Installment]) -> int:
    return sum(item.amount_cents - item.paid_cents for item in installments)


def apply_payment(session: Session, *, loan_id: uuid.UUID, amount_cents: int) -> PaymentResult:
    """Apply a payment to a loan, oldest unpaid installment first.

    The loan row is locked with SELECT ... FOR UPDATE before the balance is
    read, so a second payment on the same loan waits here and then sees the
    first one's effect rather than deciding against a stale balance.
    """
    view = load_loan(session, loan_id, for_update=True)
    loan, installments = view.loan, view.installments

    if loan.status is LoanStatus.CLOSED:
        raise LoanClosed(
            "This loan is already paid in full and accepts no further payments.",
            loan_id=str(loan_id),
        )

    outstanding = outstanding_cents(installments)
    if amount_cents > outstanding:
        raise Overpayment(
            "Payment exceeds the outstanding balance on this loan.",
            amount_cents=amount_cents,
            outstanding_cents=outstanding,
        )

    transaction_id = ledger.post(
        session,
        loan_id=loan.id,
        postings=[
            ledger.Posting(LedgerAccount.CASH, EntryDirection.DEBIT, amount_cents),
            ledger.Posting(LedgerAccount.LOAN_RECEIVABLE, EntryDirection.CREDIT, amount_cents),
        ],
    )

    payment = Payment(loan_id=loan.id, amount_cents=amount_cents, transaction_id=transaction_id)
    session.add(payment)
    session.flush()

    allocations: list[Allocation] = []
    remaining = amount_cents
    for installment in installments:
        if remaining == 0:
            break
        due = installment.amount_cents - installment.paid_cents
        if due == 0:
            continue
        applied = min(due, remaining)
        installment.paid_cents += applied
        remaining -= applied
        session.add(
            PaymentAllocation(
                payment_id=payment.id, installment_id=installment.id, amount_cents=applied
            )
        )
        allocations.append(Allocation(installment=installment, amount_cents=applied))

    if outstanding - amount_cents == 0:
        loan.status = LoanStatus.CLOSED

    session.flush()
    session.refresh(payment)
    return PaymentResult(
        payment=payment, allocations=allocations, loan=loan, installments=installments
    )
