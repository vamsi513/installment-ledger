"""Ledger balance, in the application and in the database itself."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app.ledger import Posting, UnbalancedTransaction, post
from app.models import EntryDirection, LedgerAccount
from tests.helpers import create_loan, pay

INSERT_ENTRY = text(
    """
    INSERT INTO ledger_entries (id, transaction_id, loan_id, account, direction, amount_cents)
    VALUES (:id, :transaction_id, :loan_id, :account, :direction, :amount_cents)
    """
)


def _net_by_transaction(engine: Engine) -> list[tuple[str, int]]:
    """Every ledger transaction with its signed net, straight from the database."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT transaction_id,
                       SUM(CASE WHEN direction = 'debit'
                                THEN amount_cents ELSE -amount_cents END) AS net
                  FROM ledger_entries
                 GROUP BY transaction_id
                """
            )
        ).all()
    return [(str(row[0]), int(row[1])) for row in rows]


def _account_balances(engine: Engine, loan_id: str) -> dict[str, int]:
    """Signed balance per account for one loan, debits positive."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT account,
                       SUM(CASE WHEN direction = 'debit'
                                THEN amount_cents ELSE -amount_cents END) AS balance
                  FROM ledger_entries
                 WHERE loan_id = :loan_id
                 GROUP BY account
                """
            ),
            {"loan_id": loan_id},
        ).all()
    return {str(row[0]): int(row[1]) for row in rows}


def test_ledger_is_balanced_after_loan_creation(client: httpx.Client, engine: Engine) -> None:
    loan = create_loan(client, principal_cents=500_000, apr_bps=1_200, cadence="monthly")

    nets = _net_by_transaction(engine)
    assert len(nets) == 1
    assert nets[0][1] == 0

    balances = _account_balances(engine, loan["id"])
    assert balances["loan_receivable"] == loan["total_cents"]
    assert balances["funding_payable"] == -loan["principal_cents"]
    assert balances["interest_income"] == -loan["interest_cents"]
    assert sum(balances.values()) == 0


def test_ledger_is_balanced_after_a_partial_payment(client: httpx.Client, engine: Engine) -> None:
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    assert pay(client, loan["id"], 45_000).status_code == 201

    nets = _net_by_transaction(engine)
    assert len(nets) == 2
    assert all(net == 0 for _, net in nets)

    balances = _account_balances(engine, loan["id"])
    assert balances["cash"] == 45_000
    assert balances["loan_receivable"] == 120_000 - 45_000
    assert sum(balances.values()) == 0


def test_ledger_is_balanced_after_full_payoff(client: httpx.Client, engine: Engine) -> None:
    loan = create_loan(client, principal_cents=100_003, apr_bps=999, installment_count=6)
    total = loan["total_cents"]

    assert pay(client, loan["id"], total - 1).status_code == 201
    assert pay(client, loan["id"], 1).status_code == 201

    nets = _net_by_transaction(engine)
    assert len(nets) == 3
    assert all(net == 0 for _, net in nets)

    balances = _account_balances(engine, loan["id"])
    # The receivable is fully collected and the cash received equals the total.
    assert balances["loan_receivable"] == 0
    assert balances["cash"] == total
    assert sum(balances.values()) == 0


def test_every_transaction_nets_to_zero_after_a_mixed_workload(
    client: httpx.Client, engine: Engine
) -> None:
    for principal, apr, count, cadence in (
        (100_000, 0, 4, "biweekly"),
        (250_001, 1_499, 12, "monthly"),
        (999, 5_000, 2, "biweekly"),
    ):
        loan = create_loan(
            client,
            principal_cents=principal,
            apr_bps=apr,
            installment_count=count,
            cadence=cadence,
        )
        outstanding = loan["total_cents"]
        pay(client, loan["id"], outstanding // 3)
        pay(client, loan["id"], 1)

    nets = _net_by_transaction(engine)
    assert len(nets) == 9
    assert [net for _, net in nets] == [0] * 9

    with engine.connect() as connection:
        overall = connection.execute(
            text(
                """
                SELECT SUM(CASE WHEN direction = 'debit'
                                THEN amount_cents ELSE -amount_cents END)
                  FROM ledger_entries
                """
            )
        ).scalar_one()
    assert int(overall) == 0


def test_database_rejects_an_unbalanced_transaction_at_commit(
    client: httpx.Client, engine: Engine
) -> None:
    """The guarantee is in the database, not only in the service layer."""
    loan = create_loan(client)

    with pytest.raises(IntegrityError, match="does not net to zero"), engine.begin() as connection:
        connection.execute(
            INSERT_ENTRY,
            {
                "id": uuid.uuid4(),
                "transaction_id": uuid.uuid4(),
                "loan_id": loan["id"],
                "account": "cash",
                "direction": "debit",
                "amount_cents": 500,
            },
        )

    nets = _net_by_transaction(engine)
    assert len(nets) == 1
    assert nets[0][1] == 0


def test_database_accepts_a_balanced_pair_written_by_hand(
    client: httpx.Client, engine: Engine
) -> None:
    loan = create_loan(client)
    transaction_id = uuid.uuid4()

    with engine.begin() as connection:
        for account, direction in (("cash", "debit"), ("loan_receivable", "credit")):
            connection.execute(
                INSERT_ENTRY,
                {
                    "id": uuid.uuid4(),
                    "transaction_id": transaction_id,
                    "loan_id": loan["id"],
                    "account": account,
                    "direction": direction,
                    "amount_cents": 500,
                },
            )

    nets = dict(_net_by_transaction(engine))
    assert nets[str(transaction_id)] == 0


def test_database_rejects_removing_one_side_of_a_transaction(
    client: httpx.Client, engine: Engine
) -> None:
    loan = create_loan(client)
    assert pay(client, loan["id"], 1_000).status_code == 201

    with pytest.raises(IntegrityError, match="does not net to zero"), engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ledger_entries WHERE account = 'cash' AND loan_id = :loan_id"),
            {"loan_id": loan["id"]},
        )

    assert all(net == 0 for _, net in _net_by_transaction(engine))


def test_database_rejects_changing_one_amount(client: httpx.Client, engine: Engine) -> None:
    loan = create_loan(client)
    assert pay(client, loan["id"], 1_000).status_code == 201

    with pytest.raises(IntegrityError, match="does not net to zero"), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ledger_entries SET amount_cents = amount_cents + 1 "
                "WHERE account = 'cash' AND loan_id = :loan_id"
            ),
            {"loan_id": loan["id"]},
        )

    assert all(net == 0 for _, net in _net_by_transaction(engine))


def test_posting_helper_refuses_unbalanced_postings_before_writing() -> None:
    with pytest.raises(UnbalancedTransaction, match="net to 500 cents"):
        post(
            session=None,  # never reached: the balance check runs first
            loan_id=uuid.uuid4(),
            postings=[Posting(LedgerAccount.CASH, EntryDirection.DEBIT, 500)],
        )
