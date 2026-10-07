"""Plain JSON bodies.

Responses are built as JSON ready dictionaries rather than serialized from
models at the edge. The same dictionary is both returned to the caller and
stored against the idempotency key, so a replay is identical to the original
response rather than merely equivalent to it.
"""

from __future__ import annotations

from typing import Any

from .models import EntryDirection, LedgerEntry, Loan
from .services.loans import LoanView
from .services.payments import PaymentResult, outstanding_cents


def installment_body(installment: Any) -> dict[str, Any]:
    return {
        "sequence": installment.sequence,
        "due_date": installment.due_date.isoformat(),
        "amount_cents": installment.amount_cents,
        "paid_cents": installment.paid_cents,
        "outstanding_cents": installment.amount_cents - installment.paid_cents,
    }


def loan_body(view: LoanView) -> dict[str, Any]:
    loan: Loan = view.loan
    paid = sum(item.paid_cents for item in view.installments)
    return {
        "id": str(loan.id),
        "status": loan.status.value,
        "principal_cents": loan.principal_cents,
        "apr_bps": loan.apr_bps,
        "installment_count": loan.installment_count,
        "cadence": loan.cadence.value,
        "start_date": loan.start_date.isoformat(),
        "term_days": loan.term_days,
        "interest_cents": loan.interest_cents,
        "total_cents": loan.total_cents,
        "paid_cents": paid,
        "outstanding_cents": outstanding_cents(view.installments),
        "created_at": loan.created_at.isoformat(),
        "installments": [installment_body(item) for item in view.installments],
    }


def payment_body(result: PaymentResult) -> dict[str, Any]:
    return {
        "id": str(result.payment.id),
        "loan_id": str(result.payment.loan_id),
        "amount_cents": result.payment.amount_cents,
        "transaction_id": str(result.payment.transaction_id),
        "created_at": result.payment.created_at.isoformat(),
        "allocations": [
            {
                "installment_sequence": allocation.installment.sequence,
                "installment_id": str(allocation.installment.id),
                "amount_cents": allocation.amount_cents,
            }
            for allocation in result.allocations
        ],
        "loan": loan_body(LoanView(loan=result.loan, installments=result.installments)),
    }


def ledger_body(loan_id: str, entries: list[LedgerEntry]) -> dict[str, Any]:
    """Group entries by transaction and show that each one nets to zero."""
    transactions: list[dict[str, Any]] = []
    by_transaction: dict[str, list[LedgerEntry]] = {}
    for entry in entries:
        by_transaction.setdefault(str(entry.transaction_id), []).append(entry)

    for transaction_id, group in by_transaction.items():
        debits = sum(e.amount_cents for e in group if e.direction is EntryDirection.DEBIT)
        credits = sum(e.amount_cents for e in group if e.direction is EntryDirection.CREDIT)
        transactions.append(
            {
                "transaction_id": transaction_id,
                "created_at": group[0].created_at.isoformat(),
                "debit_cents": debits,
                "credit_cents": credits,
                "net_cents": debits - credits,
                "entries": [
                    {
                        "account": entry.account.value,
                        "direction": entry.direction.value,
                        "amount_cents": entry.amount_cents,
                    }
                    for entry in group
                ],
            }
        )

    return {
        "loan_id": loan_id,
        "debit_cents": sum(t["debit_cents"] for t in transactions),
        "credit_cents": sum(t["credit_cents"] for t in transactions),
        "net_cents": sum(t["net_cents"] for t in transactions),
        "transactions": transactions,
    }
