"""Double entry posting.

Every state change that moves money writes one ledger transaction made of two
or more entries whose debits equal their credits. The amounts are stored
positive and the direction carries the sign, so a transaction nets to zero when
``sum(debits) - sum(credits) == 0``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .events import schema as event_schema
from .models import EntryDirection, LedgerAccount, LedgerEntry, OutboxEvent


@dataclass(frozen=True)
class Posting:
    account: LedgerAccount
    direction: EntryDirection
    amount_cents: int


class UnbalancedTransaction(ValueError):
    """Raised before anything is written when postings do not net to zero."""


def net_cents(postings: list[Posting]) -> int:
    return sum(
        posting.amount_cents if posting.direction is EntryDirection.DEBIT else -posting.amount_cents
        for posting in postings
    )


def post(session: Session, *, loan_id: uuid.UUID, postings: list[Posting]) -> uuid.UUID:
    """Write one balanced transaction, queue its event, and return its id.

    The balance check here is a fast local guard. The database enforces the same
    rule independently with a deferred constraint trigger, so an unbalanced
    transaction cannot be committed even if this function is bypassed.

    The ``transaction.posted`` event is written to the outbox on this same
    session, so it is part of this transaction and nothing else. The event
    therefore exists if and only if the entries it describes were committed:
    a caller that rolls back takes the event with it, and there is no window in
    which the ledger has moved but the event is missing. No broker is contacted
    from here, which is the point of the outbox: a posting cannot be made to
    fail, or to wait, by a broker being down.

    The posting rules themselves are unchanged. This function writes one extra
    row and decides nothing new.
    """
    net = net_cents(postings)
    if net != 0:
        raise UnbalancedTransaction(f"postings net to {net} cents instead of zero")

    transaction_id = uuid.uuid4()
    session.add_all(
        [
            LedgerEntry(
                transaction_id=transaction_id,
                loan_id=loan_id,
                account=posting.account,
                direction=posting.direction,
                amount_cents=posting.amount_cents,
            )
            for posting in postings
        ]
    )
    session.add(
        OutboxEvent(
            event_type=event_schema.TRANSACTION_POSTED,
            aggregate_id=loan_id,
            transaction_id=transaction_id,
            payload=event_schema.build_payload(
                loan_id=loan_id,
                transaction_id=transaction_id,
                occurred_at=datetime.now(UTC),
                postings=[
                    (posting.account.value, posting.direction.value, posting.amount_cents)
                    for posting in postings
                ],
            ),
        )
    )
    return transaction_id
