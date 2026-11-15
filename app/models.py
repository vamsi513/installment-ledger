"""Database models.

Amount columns are ``BigInteger`` holding cents. The database carries the
invariants that must never be violated, so a bug in the service layer cannot
leave bad rows behind: amounts are positive, a loan's total equals principal
plus interest, an installment can never be paid beyond its amount, and every
ledger transaction nets to zero (enforced by a deferred constraint trigger
created in the initial migration).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .schedule import Cadence


class Base(DeclarativeBase):
    pass


class LoanStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class LedgerAccount(StrEnum):
    LOAN_RECEIVABLE = "loan_receivable"
    FUNDING_PAYABLE = "funding_payable"
    INTEREST_INCOME = "interest_income"
    CASH = "cash"


class EntryDirection(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


def _pg_enum(python_enum: type[StrEnum], name: str) -> Enum:
    """A Postgres enum column that stores the member values, not their names."""
    return Enum(
        python_enum,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


class Loan(Base):
    __tablename__ = "loans"
    __table_args__ = (
        CheckConstraint("principal_cents > 0", name="ck_loans_principal_positive"),
        CheckConstraint("apr_bps >= 0", name="ck_loans_apr_non_negative"),
        CheckConstraint("installment_count >= 2", name="ck_loans_installment_count"),
        CheckConstraint("interest_cents >= 0", name="ck_loans_interest_non_negative"),
        CheckConstraint("term_days >= 0", name="ck_loans_term_non_negative"),
        CheckConstraint(
            "total_cents = principal_cents + interest_cents", name="ck_loans_total_consistent"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    status: Mapped[LoanStatus] = mapped_column(_pg_enum(LoanStatus, "loan_status"))
    principal_cents: Mapped[int] = mapped_column(BigInteger)
    apr_bps: Mapped[int] = mapped_column(Integer)
    installment_count: Mapped[int] = mapped_column(Integer)
    cadence: Mapped[Cadence] = mapped_column(_pg_enum(Cadence, "cadence"))
    start_date: Mapped[date] = mapped_column(Date)
    term_days: Mapped[int] = mapped_column(Integer)
    interest_cents: Mapped[int] = mapped_column(BigInteger)
    total_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Installment(Base):
    __tablename__ = "installments"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_installments_sequence_positive"),
        CheckConstraint("amount_cents > 0", name="ck_installments_amount_positive"),
        CheckConstraint(
            "paid_cents >= 0 AND paid_cents <= amount_cents", name="ck_installments_paid_in_range"
        ),
        Index("uq_installments_loan_sequence", "loan_id", "sequence", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("loans.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    paid_cents: Mapped[int] = mapped_column(BigInteger, default=0)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_payments_amount_positive"),
        Index("ix_payments_loan_id", "loan_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("loans.id", ondelete="CASCADE"))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentAllocation(Base):
    """How much of a payment landed on each installment."""

    __tablename__ = "payment_allocations"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_allocations_amount_positive"),
        Index("uq_allocations_payment_installment", "payment_id", "installment_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"))
    installment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("installments.id", ondelete="CASCADE")
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger)


class LedgerEntry(Base):
    """One side of one double entry posting."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_ledger_amount_positive"),
        Index("ix_ledger_entries_transaction_id", "transaction_id"),
        Index("ix_ledger_entries_loan_id", "loan_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    loan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("loans.id", ondelete="CASCADE"))
    account: Mapped[LedgerAccount] = mapped_column(_pg_enum(LedgerAccount, "ledger_account"))
    direction: Mapped[EntryDirection] = mapped_column(_pg_enum(EntryDirection, "entry_direction"))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IdempotencyKey(Base):
    """A claimed Idempotency-Key plus the response it produced.

    The unique index on ``key`` is what actually makes replays safe. The row is
    inserted in the same transaction as the write it guards, so a concurrent
    request with the same key blocks on the index and then reads the stored
    response instead of writing a second time.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (Index("uq_idempotency_keys_key", "key", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(Text)
    endpoint: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(Text)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxEvent(Base):
    """An event waiting to be published, written in the posting transaction.

    This is the transactional outbox. The row is inserted on the same session
    and therefore in the same database transaction as the ledger entries it
    describes, so the event exists if and only if the transaction committed.
    No broker is touched while that transaction is open.

    ``published_at`` is the only mutable column: null means unsent. The
    publisher sets it *after* the broker has accepted the message, so a crash
    between those two steps leaves the row unsent and it is published again on
    restart. That is what makes delivery at least once rather than at most
    once, and it is why the consumer has to be idempotent.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts_non_negative"),
        Index("uq_outbox_events_event_id", "event_id", unique=True),
        # The publisher's only query is "oldest unsent rows first", so the
        # index is partial on exactly those rows.
        Index(
            "ix_outbox_events_unpublished",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(Text)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    payload: Mapped[dict] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )


class ProcessedEvent(Base):
    """One row per (consumer, event) pair that has been handled.

    This is what makes a duplicate delivery a no-op, and like the idempotency
    keys it rests on a unique index rather than an application level check. The
    consumer inserts this row in the same transaction as its read model update,
    so a redelivered event hits the index, the transaction rolls back, and the
    read model is left exactly as it was.
    """

    __tablename__ = "processed_events"
    __table_args__ = (
        Index("uq_processed_events_consumer_event", "consumer", "event_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    consumer: Mapped[str] = mapped_column(Text)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AccountTotal(Base):
    """Per account running totals, maintained by the consumer.

    A read model, not a source of truth: it is derived from the events and can
    be rebuilt by replaying them. Debits and credits are kept separately rather
    than as one signed net so that the pair can be checked against the ledger
    itself, and because a running total that only ever increases is easier to
    reason about under redelivery.
    """

    __tablename__ = "account_totals"
    __table_args__ = (
        CheckConstraint("debit_cents >= 0", name="ck_account_totals_debit_non_negative"),
        CheckConstraint("credit_cents >= 0", name="ck_account_totals_credit_non_negative"),
    )

    account: Mapped[LedgerAccount] = mapped_column(
        _pg_enum(LedgerAccount, "ledger_account"), primary_key=True
    )
    debit_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    credit_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
