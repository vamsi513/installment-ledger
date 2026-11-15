"""The wire format for ``transaction.posted``.

One event per posted ledger transaction, carrying the postings that made it up.
The consumer needs the postings to keep per account totals, and carrying them
on the event means the read model never has to read back the ledger tables.

Amounts stay integer cents on the wire, as they are everywhere else in this
service. ``direction`` carries the sign, exactly as in ``ledger_entries``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

TRANSACTION_POSTED = "transaction.posted"


class MalformedEvent(ValueError):
    """Raised when a message cannot be read as an event.

    A message that cannot be parsed cannot be retried into success, but it is
    still put through the consumer's bounded retries and then dead lettered,
    because the consumer deliberately has one path out for everything it
    cannot handle rather than two.
    """


@dataclass(frozen=True)
class PostingPayload:
    account: str
    direction: str
    amount_cents: int


@dataclass(frozen=True)
class TransactionPosted:
    event_id: uuid.UUID
    occurred_at: datetime
    loan_id: uuid.UUID
    transaction_id: uuid.UUID
    postings: tuple[PostingPayload, ...]

    @property
    def net_cents(self) -> int:
        return sum(
            posting.amount_cents if posting.direction == "debit" else -posting.amount_cents
            for posting in self.postings
        )


def build_payload(
    *,
    loan_id: uuid.UUID,
    transaction_id: uuid.UUID,
    occurred_at: datetime,
    postings: list[tuple[str, str, int]],
) -> dict[str, Any]:
    """The JSON body stored in ``outbox_events.payload``.

    Kept as plain JSON types so the column round trips unchanged and so the
    payload is readable in the database without the application present.
    """
    return {
        "loan_id": str(loan_id),
        "transaction_id": str(transaction_id),
        "occurred_at": occurred_at.isoformat(),
        "postings": [
            {"account": account, "direction": direction, "amount_cents": amount_cents}
            for account, direction, amount_cents in postings
        ],
    }


def encode(*, event_id: uuid.UUID, event_type: str, payload: dict[str, Any]) -> bytes:
    """Serialise an envelope for the broker.

    Sorted keys and no spaces, so the same event always produces the same bytes
    and a message can be compared by value in a test.
    """
    envelope = {"event_id": str(event_id), "event_type": event_type, "payload": payload}
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()


def decode(body: bytes) -> TransactionPosted:
    """Read a message back, or raise ``MalformedEvent``.

    Every failure mode collapses to one exception type on purpose: the consumer
    treats anything it cannot read as a message to retry and then dead letter,
    and it should not have to know which part of the parse went wrong.
    """
    try:
        envelope = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise MalformedEvent(f"message body is not JSON: {exc}") from exc

    if not isinstance(envelope, dict):
        raise MalformedEvent("message body is not a JSON object")

    event_type = envelope.get("event_type")
    if event_type != TRANSACTION_POSTED:
        raise MalformedEvent(f"unsupported event_type {event_type!r}")

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise MalformedEvent("payload is missing or not an object")

    try:
        event_id = uuid.UUID(str(envelope["event_id"]))
        loan_id = uuid.UUID(str(payload["loan_id"]))
        transaction_id = uuid.UUID(str(payload["transaction_id"]))
        occurred_at = datetime.fromisoformat(str(payload["occurred_at"]))
        raw_postings = payload["postings"]
    except (KeyError, ValueError, TypeError) as exc:
        raise MalformedEvent(f"event is missing or has a malformed field: {exc}") from exc

    if not isinstance(raw_postings, list) or not raw_postings:
        raise MalformedEvent("postings must be a non empty list")

    postings: list[PostingPayload] = []
    for item in raw_postings:
        if not isinstance(item, dict):
            raise MalformedEvent("each posting must be an object")
        account = item.get("account")
        direction = item.get("direction")
        amount = item.get("amount_cents")
        if direction not in ("debit", "credit"):
            raise MalformedEvent(f"unknown direction {direction!r}")
        # bool is an int subclass, and True would silently become 1 cent.
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise MalformedEvent(f"amount_cents must be a positive integer, got {amount!r}")
        if not isinstance(account, str) or not account:
            raise MalformedEvent(f"account must be a non empty string, got {account!r}")
        postings.append(PostingPayload(account=account, direction=direction, amount_cents=amount))

    return TransactionPosted(
        event_id=event_id,
        occurred_at=occurred_at,
        loan_id=loan_id,
        transaction_id=transaction_id,
        postings=tuple(postings),
    )
