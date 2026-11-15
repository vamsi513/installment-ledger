"""Transactional outbox, consumer idempotency and the per account read model.

Revision ID: 0002
Revises: 0001

Three new tables and no change to any existing one. The ledger's own schema,
its constraints and its balance trigger are untouched, so posting behaves
exactly as it did before.

``ledger_account`` already exists as a Postgres enum type, created by 0001, so
``account_totals`` reuses it with ``create_type=False`` and the downgrade here
leaves the type alone for 0001 to drop.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEDGER_ACCOUNT = postgresql.ENUM(
    "loan_receivable",
    "funding_payable",
    "interest_income",
    "cash",
    name="ledger_account",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_attempts_non_negative"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_outbox_events_event_id", "outbox_events", ["event_id"], unique=True)
    op.create_index(
        "ix_outbox_events_unpublished",
        "outbox_events",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "processed_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_processed_events_consumer_event",
        "processed_events",
        ["consumer", "event_id"],
        unique=True,
    )

    op.create_table(
        "account_totals",
        sa.Column("account", LEDGER_ACCOUNT, nullable=False),
        sa.Column("debit_cents", sa.BigInteger(), nullable=False),
        sa.Column("credit_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("debit_cents >= 0", name="ck_account_totals_debit_non_negative"),
        sa.CheckConstraint("credit_cents >= 0", name="ck_account_totals_credit_non_negative"),
        sa.PrimaryKeyConstraint("account"),
    )


def downgrade() -> None:
    op.drop_table("account_totals")
    op.drop_index("uq_processed_events_consumer_event", table_name="processed_events")
    op.drop_table("processed_events")
    op.drop_index("ix_outbox_events_unpublished", table_name="outbox_events")
    op.drop_index("uq_outbox_events_event_id", table_name="outbox_events")
    op.drop_table("outbox_events")
