"""The consumer: ``transaction.posted`` into per account running totals.

Two properties carry this module.

**A duplicate delivery changes nothing.** The guard is a unique index on
``(consumer, event_id)``, not an application level check, so it holds under
concurrency and against a second worker rather than only against a sequential
redelivery. The marker row and the read model update are one transaction: either
the event is marked handled and the totals moved, or neither happened. A
redelivery hits the index, the transaction rolls back whole, and the totals are
left exactly as they were.

**A message that cannot be handled ends up somewhere.** Each message is
attempted a bounded number of times and then published to the dead letter
queue and acked, so a poison message cannot block the queue behind it forever.
"""

from __future__ import annotations

import enum
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..db import get_sessionmaker
from ..models import AccountTotal, LedgerAccount, ProcessedEvent
from . import config
from .broker import Broker, Message
from .schema import TransactionPosted, decode

logger = logging.getLogger("app.events.consumer")


class Outcome(enum.StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True)
class HandledMessage:
    outcome: Outcome
    message: Message
    error: str | None = None


class DuplicateEvent(Exception):
    """The unique index rejected the marker row, so this event was already handled."""


def apply_to_read_model(session: Session, event: TransactionPosted) -> None:
    """Fold one event's postings into ``account_totals``.

    An upsert per account rather than a read then write, so two workers
    handling different events for the same account cannot lose one of the two
    increments to a stale read.

    An account on an event that is not one this service knows is a genuine
    error rather than something to skip: it means the event and this code
    disagree about the chart of accounts, and silently dropping it would leave
    the read model quietly wrong.
    """
    known = {account.value for account in LedgerAccount}
    for posting in event.postings:
        if posting.account not in known:
            raise ValueError(f"event names unknown account {posting.account!r}")

        debit = posting.amount_cents if posting.direction == "debit" else 0
        credit = posting.amount_cents if posting.direction == "credit" else 0
        statement = insert(AccountTotal).values(
            account=posting.account, debit_cents=debit, credit_cents=credit
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[AccountTotal.account],
                set_={
                    "debit_cents": AccountTotal.debit_cents + debit,
                    "credit_cents": AccountTotal.credit_cents + credit,
                },
            )
        )


def handle_event(
    event: TransactionPosted,
    *,
    sessions: sessionmaker[Session] | None = None,
    consumer: str | None = None,
) -> None:
    """Mark the event handled and move the totals, in one transaction.

    Raises ``DuplicateEvent`` if this consumer has already handled it, having
    changed nothing.
    """
    sessions = sessions or get_sessionmaker()
    consumer = consumer or config.consumer_name()

    try:
        with sessions() as session, session.begin():
            session.add(ProcessedEvent(consumer=consumer, event_id=event.event_id))
            try:
                session.flush()
            except IntegrityError as exc:
                raise DuplicateEvent from exc
            apply_to_read_model(session, event)
    except DuplicateEvent:
        # The context manager has rolled the transaction back, so the read
        # model is untouched. Re raise for the caller to ack and move on.
        raise


def handle_once(
    broker: Broker,
    message: Message,
    *,
    sessions: sessionmaker[Session] | None = None,
    consumer: str | None = None,
    queue: str | None = None,
    dead_letter_queue: str | None = None,
    max_attempts: int | None = None,
    backoff_seconds: float | None = None,
    sleep: Callable[[float], None] | None = None,
) -> HandledMessage:
    """Handle one message to a conclusion, then ack it.

    The message is always acked, because every path out of here is final: it
    was applied, it was a duplicate, or it was dead lettered. Nothing is left
    for the broker to redeliver, which is what keeps a poison message from
    blocking the queue.
    """
    attempts = max_attempts or config.max_attempts()
    backoff = config.retry_backoff_seconds() if backoff_seconds is None else backoff_seconds
    pause = sleep or time.sleep
    dlq = dead_letter_queue or config.dead_letter_queue_name()

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            event = decode(message.body)
            handle_event(event, sessions=sessions, consumer=consumer)
        except DuplicateEvent:
            broker.ack(message)
            logger.info("event %s already handled; nothing changed", message.message_id)
            return HandledMessage(outcome=Outcome.DUPLICATE, message=message)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "attempt %s of %s failed for message %s: %s",
                attempt,
                attempts,
                message.message_id,
                exc,
            )
            if attempt < attempts and backoff > 0:
                pause(backoff)
        else:
            broker.ack(message)
            return HandledMessage(outcome=Outcome.APPLIED, message=message)

    reason = f"{type(last_error).__name__}: {last_error}"
    broker.publish(dlq, Message(message_id=message.message_id, body=message.body))
    broker.ack(message)
    logger.error(
        "message %s dead lettered to %s after %s attempts: %s",
        message.message_id,
        dlq,
        attempts,
        reason,
    )
    return HandledMessage(outcome=Outcome.DEAD_LETTERED, message=message, error=reason)


def run(
    broker: Broker,
    *,
    sessions: sessionmaker[Session] | None = None,
    consumer: str | None = None,
    queue: str | None = None,
    dead_letter_queue: str | None = None,
    stop: threading.Event | None = None,
    poll_seconds: float | None = None,
    max_attempts: int | None = None,
) -> None:
    """Consume until ``stop`` is set."""
    stop = stop or threading.Event()
    source = queue or config.queue_name()
    interval = config.poll_seconds() if poll_seconds is None else poll_seconds

    while not stop.is_set():
        message = broker.receive(source, timeout_seconds=interval)
        if message is None:
            continue
        try:
            handle_once(
                broker,
                message,
                sessions=sessions,
                consumer=consumer,
                queue=source,
                dead_letter_queue=dead_letter_queue,
                max_attempts=max_attempts,
            )
        except Exception:
            # Deliberately not acked: this is a failure of the handling
            # machinery itself, not of the message, so let the broker
            # redeliver it rather than lose it.
            logger.exception("message %s left unacked for redelivery", message.message_id)


def main() -> int:  # pragma: no cover - process entry point
    logging.basicConfig(
        level=logging.INFO,
        format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )
    from .rabbit import RabbitBroker

    stop = threading.Event()
    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, lambda *_: stop.set())

    broker = RabbitBroker()
    logger.info("consumer %s started on queue %s", config.consumer_name(), config.queue_name())
    try:
        run(broker, stop=stop)
    finally:
        broker.close()
    logger.info("consumer stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
