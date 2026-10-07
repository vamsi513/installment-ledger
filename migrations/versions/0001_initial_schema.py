"""Initial schema with the deferred ledger balance trigger.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOAN_STATUS = postgresql.ENUM("open", "closed", name="loan_status", create_type=False)
CADENCE = postgresql.ENUM("biweekly", "monthly", name="cadence", create_type=False)
LEDGER_ACCOUNT = postgresql.ENUM(
    "loan_receivable",
    "funding_payable",
    "interest_income",
    "cash",
    name="ledger_account",
    create_type=False,
)
ENTRY_DIRECTION = postgresql.ENUM("debit", "credit", name="entry_direction", create_type=False)

# A deferred constraint trigger, so the check runs once at COMMIT when every
# entry of the transaction is already present. An unbalanced ledger transaction
# therefore cannot be committed, whatever the application layer does.
BALANCE_FUNCTION = """
CREATE FUNCTION ledger_transaction_balanced() RETURNS trigger AS $$
DECLARE
    affected_transaction uuid;
    net bigint;
BEGIN
    affected_transaction := COALESCE(NEW.transaction_id, OLD.transaction_id);
    SELECT COALESCE(SUM(
               CASE WHEN direction::text = 'debit'
                    THEN amount_cents
                    ELSE -amount_cents
               END), 0)
      INTO net
      FROM ledger_entries
     WHERE transaction_id = affected_transaction;

    IF net <> 0 THEN
        RAISE EXCEPTION
            'ledger transaction % does not net to zero (net = % cents)',
            affected_transaction, net
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

BALANCE_TRIGGER = """
CREATE CONSTRAINT TRIGGER ledger_entries_must_balance
AFTER INSERT OR UPDATE OR DELETE ON ledger_entries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION ledger_transaction_balanced();
"""


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in (LOAN_STATUS, CADENCE, LEDGER_ACCOUNT, ENTRY_DIRECTION):
        enum_type.create(bind, checkfirst=False)

    op.create_table(
        "loans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", LOAN_STATUS, nullable=False),
        sa.Column("principal_cents", sa.BigInteger(), nullable=False),
        sa.Column("apr_bps", sa.Integer(), nullable=False),
        sa.Column("installment_count", sa.Integer(), nullable=False),
        sa.Column("cadence", CADENCE, nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("term_days", sa.Integer(), nullable=False),
        sa.Column("interest_cents", sa.BigInteger(), nullable=False),
        sa.Column("total_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("principal_cents > 0", name="ck_loans_principal_positive"),
        sa.CheckConstraint("apr_bps >= 0", name="ck_loans_apr_non_negative"),
        sa.CheckConstraint("installment_count >= 2", name="ck_loans_installment_count"),
        sa.CheckConstraint("interest_cents >= 0", name="ck_loans_interest_non_negative"),
        sa.CheckConstraint("term_days >= 0", name="ck_loans_term_non_negative"),
        sa.CheckConstraint(
            "total_cents = principal_cents + interest_cents", name="ck_loans_total_consistent"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "installments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("paid_cents", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_installments_sequence_positive"),
        sa.CheckConstraint("amount_cents > 0", name="ck_installments_amount_positive"),
        sa.CheckConstraint(
            "paid_cents >= 0 AND paid_cents <= amount_cents", name="ck_installments_paid_in_range"
        ),
        sa.ForeignKeyConstraint(["loan_id"], ["loans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_installments_loan_sequence", "installments", ["loan_id", "sequence"], unique=True
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_payments_amount_positive"),
        sa.ForeignKeyConstraint(["loan_id"], ["loans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payments_loan_id", "payments", ["loan_id"], unique=False)

    op.create_table(
        "payment_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("installment_id", sa.Uuid(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("amount_cents > 0", name="ck_allocations_amount_positive"),
        sa.ForeignKeyConstraint(["installment_id"], ["installments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_allocations_payment_installment",
        "payment_allocations",
        ["payment_id", "installment_id"],
        unique=True,
    )

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("account", LEDGER_ACCOUNT, nullable=False),
        sa.Column("direction", ENTRY_DIRECTION, nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_ledger_amount_positive"),
        sa.ForeignKeyConstraint(["loan_id"], ["loans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ledger_entries_loan_id", "ledger_entries", ["loan_id"], unique=False)
    op.create_index(
        "ix_ledger_entries_transaction_id", "ledger_entries", ["transaction_id"], unique=False
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_idempotency_keys_key", "idempotency_keys", ["key"], unique=True)

    op.execute(BALANCE_FUNCTION)
    op.execute(BALANCE_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ledger_entries_must_balance ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS ledger_transaction_balanced()")

    op.drop_index("uq_idempotency_keys_key", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
    op.drop_index("ix_ledger_entries_transaction_id", table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_loan_id", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_index("uq_allocations_payment_installment", table_name="payment_allocations")
    op.drop_table("payment_allocations")
    op.drop_index("ix_payments_loan_id", table_name="payments")
    op.drop_table("payments")
    op.drop_index("uq_installments_loan_sequence", table_name="installments")
    op.drop_table("installments")
    op.drop_table("loans")

    bind = op.get_bind()
    for enum_type in (ENTRY_DIRECTION, LEDGER_ACCOUNT, CADENCE, LOAN_STATUS):
        enum_type.drop(bind, checkfirst=False)
