"""Typed results for the MCP tools.

Every tool returns one of these models, so the tool has a real output schema and
the agent branches on a ``status`` field rather than on prose.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ToolStatus = Literal["ok", "rejected", "unavailable", "unknown"]


class InstallmentLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(description="Position in the schedule, starting at 1.")
    due_date: str = Field(description="ISO date the installment falls due.")
    amount_cents: int = Field(description="Amount of the installment, in integer cents.")
    paid_cents: int = Field(description="How much of it has been paid, in integer cents.")
    outstanding_cents: int = Field(description="What is still owed on it, in integer cents.")


class LoanSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus = Field(description="Outcome of the call, not of the loan.")
    error_code: str | None = Field(default=None, description="Ledger error code when rejected.")
    message: str | None = Field(default=None, description="Human readable detail when not ok.")
    loan_id: str | None = None
    loan_status: Literal["open", "closed"] | None = Field(
        default=None, description="Whether the loan still accepts payments."
    )
    total_cents: int | None = Field(default=None, description="Total payable, in integer cents.")
    paid_cents: int | None = Field(default=None, description="Paid so far, in integer cents.")
    outstanding_cents: int | None = Field(
        default=None, description="Remaining balance, in integer cents."
    )
    installment_count: int | None = None
    installments: list[InstallmentLine] = Field(default_factory=list)


class LedgerLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str
    created_at: str
    account: str = Field(
        description="One of loan_receivable, funding_payable, interest_income, cash."
    )
    direction: Literal["debit", "credit"]
    amount_cents: int = Field(description="Always positive; the direction carries the sign.")


class LedgerLines(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    error_code: str | None = None
    message: str | None = None
    loan_id: str | None = None
    net_cents: int | None = Field(
        default=None, description="Debits minus credits over every entry; zero on a sound ledger."
    )
    returned: int = Field(default=0, description="How many lines are in this response.")
    lines: list[LedgerLine] = Field(default_factory=list)


class CoveredInstallment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    due_date: str
    applied_cents: int = Field(description="How much of the payment would land here.")
    clears_installment: bool = Field(
        description="True when this payment would pay the installment off completely."
    )


class PaymentPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    error_code: str | None = None
    message: str | None = None
    loan_id: str | None = None
    amount_cents: int | None = None
    outstanding_before_cents: int | None = None
    outstanding_after_cents: int | None = None
    covers: list[CoveredInstallment] = Field(default_factory=list)
    pending_action_id: str | None = Field(
        default=None, description="Pass this to confirm_payment to actually apply the payment."
    )
    expires_at_epoch_seconds: float | None = Field(
        default=None, description="After this time the pending action can no longer be confirmed."
    )


class PaymentOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    error_code: str | None = None
    message: str | None = None
    loan_id: str | None = None
    payment_id: str | None = None
    amount_cents: int | None = None
    outstanding_after_cents: int | None = None
    loan_status: Literal["open", "closed"] | None = None
    applied: bool = Field(
        default=False,
        description="True only when the ledger confirmed the payment. False when the "
        "outcome is unknown, such as after a timeout.",
    )
