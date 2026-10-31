"""The MCP tool server, spoken over stdio.

Four tools, three of which only read. ``confirm_payment`` is the only one that
writes anything, and all it does is call the ledger's own payments endpoint with
an ``Idempotency-Key`` derived from the pending action id, so confirming twice
or retrying after a timeout applies the payment exactly once.

The agent reaches these through ``tools/list`` and ``tools/call``. It does not
import this module.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..config import Settings, get_settings
from ..ledger_client import (
    LedgerClient,
    LedgerRejected,
    LedgerTimeout,
    LedgerUnavailable,
)
from ..pending import PendingActionExpired, PendingActionNotFound, PendingActionStore
from ..redaction import redact_loan_id
from .schemas import (
    CoveredInstallment,
    InstallmentLine,
    LedgerLine,
    LedgerLines,
    LoanSummary,
    PaymentOutcome,
    PaymentPreview,
)

logger = logging.getLogger("assistant.mcp")

SERVER_NAME = "installment-ledger-tools"
TOOL_NAMES = ("get_loan_summary", "get_ledger_entries", "prepare_payment", "confirm_payment")

MAX_LEDGER_LINES = 100

LoanIdArg = Annotated[
    str,
    Field(description="The loan's UUID, exactly as the ledger returned it.", min_length=1),
]
AmountArg = Annotated[
    int,
    Field(description="Payment amount as a positive integer number of cents. Never a float."),
]
LimitArg = Annotated[
    int,
    Field(
        description="How many of the most recent ledger lines to return.", ge=1, le=MAX_LEDGER_LINES
    ),
]
PendingIdArg = Annotated[
    str,
    Field(description="A pending_action_id returned by prepare_payment.", min_length=1),
]


def plan_allocation(
    installments: list[dict[str, Any]], amount_cents: int
) -> list[CoveredInstallment]:
    """Work out which installments a payment would land on.

    Mirrors the ledger's rule of filling the oldest unpaid installment first.
    This is a preview only; the ledger does the real allocation when the payment
    is posted, and its answer is the one that counts.
    """
    remaining = amount_cents
    covered: list[CoveredInstallment] = []
    for item in sorted(installments, key=lambda row: int(row["sequence"])):
        if remaining <= 0:
            break
        due = int(item["outstanding_cents"])
        if due == 0:
            continue
        applied = min(due, remaining)
        covered.append(
            CoveredInstallment(
                sequence=int(item["sequence"]),
                due_date=str(item["due_date"]),
                applied_cents=applied,
                clears_installment=applied == due,
            )
        )
        remaining -= applied
    return covered


def build_server(settings: Settings | None = None) -> MCPServer:
    """Wire the tools against a ledger client and a pending action store."""
    resolved = settings or get_settings()
    ledger = LedgerClient(resolved.ledger_base_url, timeout_seconds=resolved.ledger_timeout_seconds)
    pending = PendingActionStore(resolved.state_db_path, ttl_seconds=resolved.pending_ttl_seconds)

    server = MCPServer(
        name=SERVER_NAME,
        version="0.1.0",
        instructions=(
            "Tools for one borrower's installment loans. Amounts are always integer "
            "cents. Reading is free; writing requires prepare_payment followed by "
            "confirm_payment with the pending_action_id it returned."
        ),
    )

    @server.tool(
        name="get_loan_summary",
        title="Get loan summary",
        description=(
            "Read a loan's outstanding balance, its installment schedule and whether it "
            "is still open. Writes nothing."
        ),
    )
    async def get_loan_summary(loan_id: LoanIdArg) -> LoanSummary:
        try:
            loan = await ledger.get_loan(loan_id)
        except LedgerRejected as exc:
            return LoanSummary(status="rejected", error_code=exc.code, message=exc.message)
        except LedgerTimeout as exc:
            return LoanSummary(status="unknown", message=str(exc))
        except LedgerUnavailable as exc:
            return LoanSummary(status="unavailable", message=str(exc))

        return LoanSummary(
            status="ok",
            loan_id=loan["id"],
            loan_status=loan["status"],
            total_cents=loan["total_cents"],
            paid_cents=loan["paid_cents"],
            outstanding_cents=loan["outstanding_cents"],
            installment_count=loan["installment_count"],
            installments=[
                InstallmentLine(
                    sequence=item["sequence"],
                    due_date=item["due_date"],
                    amount_cents=item["amount_cents"],
                    paid_cents=item["paid_cents"],
                    outstanding_cents=item["outstanding_cents"],
                )
                for item in loan["installments"]
            ],
        )

    @server.tool(
        name="get_ledger_entries",
        title="Get recent ledger entries",
        description=(
            "Read the most recent double entry ledger lines for a loan, newest first. "
            "Each line is one side of one posting. Writes nothing."
        ),
    )
    async def get_ledger_entries(loan_id: LoanIdArg, limit: LimitArg = 20) -> LedgerLines:
        try:
            body = await ledger.get_ledger(loan_id)
        except LedgerRejected as exc:
            return LedgerLines(status="rejected", error_code=exc.code, message=exc.message)
        except LedgerTimeout as exc:
            return LedgerLines(status="unknown", message=str(exc))
        except LedgerUnavailable as exc:
            return LedgerLines(status="unavailable", message=str(exc))

        lines: list[LedgerLine] = []
        for transaction in reversed(body["transactions"]):
            for entry in transaction["entries"]:
                lines.append(
                    LedgerLine(
                        transaction_id=transaction["transaction_id"],
                        created_at=transaction["created_at"],
                        account=entry["account"],
                        direction=entry["direction"],
                        amount_cents=entry["amount_cents"],
                    )
                )
        trimmed = lines[:limit]
        return LedgerLines(
            status="ok",
            loan_id=body["loan_id"],
            net_cents=body["net_cents"],
            returned=len(trimmed),
            lines=trimmed,
        )

    @server.tool(
        name="prepare_payment",
        title="Prepare a payment for confirmation",
        description=(
            "Validate a payment and return a preview of what it would do, without "
            "writing anything. Returns a single use pending_action_id with an expiry; "
            "pass that to confirm_payment to actually apply the payment."
        ),
    )
    async def prepare_payment(loan_id: LoanIdArg, amount_cents: AmountArg) -> PaymentPreview:
        if amount_cents <= 0:
            return PaymentPreview(
                status="rejected",
                error_code="invalid_amount",
                message="A payment amount must be a positive number of cents.",
            )
        if amount_cents > resolved.max_payment_cents:
            return PaymentPreview(
                status="rejected",
                error_code="amount_above_cap",
                message=(
                    f"A single payment through the assistant is capped at "
                    f"{resolved.max_payment_cents} cents."
                ),
            )

        try:
            loan = await ledger.get_loan(loan_id)
        except LedgerRejected as exc:
            return PaymentPreview(status="rejected", error_code=exc.code, message=exc.message)
        except LedgerTimeout as exc:
            return PaymentPreview(status="unknown", message=str(exc))
        except LedgerUnavailable as exc:
            return PaymentPreview(status="unavailable", message=str(exc))

        outstanding = int(loan["outstanding_cents"])
        if loan["status"] == "closed":
            return PaymentPreview(
                status="rejected",
                error_code="loan_closed",
                message="This loan is paid off and takes no further payments.",
                loan_id=loan["id"],
            )
        if amount_cents > outstanding:
            return PaymentPreview(
                status="rejected",
                error_code="overpayment",
                message=(
                    f"That is more than the {outstanding} cents still outstanding on this loan."
                ),
                loan_id=loan["id"],
                amount_cents=amount_cents,
                outstanding_before_cents=outstanding,
            )

        action = pending.create(loan_id=loan["id"], amount_cents=amount_cents)
        logger.info(
            "prepared payment",
            extra={
                "loan": redact_loan_id(loan["id"]),
                "amount_cents": amount_cents,
                "pending_action_id": action.pending_action_id,
            },
        )
        return PaymentPreview(
            status="ok",
            loan_id=loan["id"],
            amount_cents=amount_cents,
            outstanding_before_cents=outstanding,
            outstanding_after_cents=outstanding - amount_cents,
            covers=plan_allocation(loan["installments"], amount_cents),
            pending_action_id=action.pending_action_id,
            expires_at_epoch_seconds=action.expires_at,
        )

    @server.tool(
        name="confirm_payment",
        title="Confirm a prepared payment",
        description=(
            "Apply a payment that prepare_payment already validated. This is the only "
            "tool that writes. The ledger request carries an Idempotency-Key derived "
            "from the pending_action_id, so calling this twice with the same id, or "
            "retrying it after a timeout, applies the payment exactly once."
        ),
    )
    async def confirm_payment(pending_action_id: PendingIdArg) -> PaymentOutcome:
        try:
            action = pending.require_live(pending_action_id)
        except PendingActionNotFound:
            return PaymentOutcome(
                status="rejected",
                error_code="pending_action_not_found",
                message="There is no prepared payment with that id.",
            )
        except PendingActionExpired:
            return PaymentOutcome(
                status="rejected",
                error_code="pending_action_expired",
                message="That prepared payment has expired. Prepare it again.",
            )

        try:
            payment = await ledger.create_payment(
                action.loan_id,
                action.amount_cents,
                idempotency_key=action.idempotency_key,
            )
        except LedgerRejected as exc:
            return PaymentOutcome(
                status="rejected",
                error_code=exc.code,
                message=exc.message,
                loan_id=action.loan_id,
                amount_cents=action.amount_cents,
            )
        except LedgerTimeout as exc:
            # The write may have landed. Saying otherwise would be a lie, and
            # retrying blindly is the caller's decision, not this tool's.
            return PaymentOutcome(
                status="unknown",
                error_code="ledger_timeout",
                message=str(exc),
                loan_id=action.loan_id,
                amount_cents=action.amount_cents,
                applied=False,
            )
        except LedgerUnavailable as exc:
            return PaymentOutcome(
                status="unavailable",
                error_code="ledger_unavailable",
                message=str(exc),
                loan_id=action.loan_id,
                amount_cents=action.amount_cents,
                applied=False,
            )

        pending.mark_consumed(pending_action_id)
        loan = payment["loan"]
        logger.info(
            "payment applied",
            extra={
                "loan": redact_loan_id(action.loan_id),
                "amount_cents": action.amount_cents,
                "payment_id": payment["id"],
            },
        )
        return PaymentOutcome(
            status="ok",
            loan_id=action.loan_id,
            payment_id=payment["id"],
            amount_cents=payment["amount_cents"],
            outstanding_after_cents=loan["outstanding_cents"],
            loan_status=loan["status"],
            applied=True,
        )

    return server
