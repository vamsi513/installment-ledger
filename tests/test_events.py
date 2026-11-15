"""The outbox, the publisher and the consumer.

Real Postgres, because the guarantees being tested are database guarantees: the
outbox row sharing the posting transaction, and the unique index that makes a
redelivery a no op. Neither can be demonstrated against a stand in.

The broker is the in memory double. The repo runs its tests against a real
Postgres and has never had a broker, so there is no container here to reuse;
the double sits behind the same ``Broker`` interface the RabbitMQ adapter
implements, and the publisher and consumer under test are the same code either
way. What the double cannot show is covered in the README rather than asserted
here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import httpx
import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app import ledger
from app.events import consumer, publisher
from app.events.broker import InMemoryBroker, Message
from app.events.consumer import Outcome
from app.events.schema import TRANSACTION_POSTED, decode, encode
from app.models import (
    AccountTotal,
    EntryDirection,
    LedgerAccount,
    LedgerEntry,
    OutboxEvent,
    ProcessedEvent,
)
from app.services.loans import create_loan
from app.services.payments import apply_payment
from tests.helpers import create_loan as http_create_loan
from tests.helpers import pay

QUEUE = "test.transaction.posted"
DLQ = "test.transaction.posted.dlq"
CONSUMER = "test-account-totals"

# Cleared here rather than in tests/conftest.py: these tables are this module's
# business, and the shared fixture is left exactly as it was.
EVENT_TABLES = ("outbox_events", "processed_events", "account_totals")


@pytest.fixture
def clear_event_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(EVENT_TABLES)} CASCADE"))


@pytest.fixture
def sessions(engine: Engine, clear_event_tables: None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def broker() -> Iterator[InMemoryBroker]:
    instance = InMemoryBroker()
    yield instance
    instance.close()


def _make_loan(session: Session) -> uuid.UUID:
    view = create_loan(
        session,
        principal_cents=120_000,
        apr_bps=0,
        installment_count=4,
        cadence="biweekly",
        start_date=date(2026, 1, 15),
    )
    return view.loan.id


def _outbox(session: Session) -> list[OutboxEvent]:
    return list(
        session.execute(
            select(OutboxEvent).order_by(OutboxEvent.created_at, OutboxEvent.id)
        ).scalars()
    )


# --- the outbox shares the posting transaction ---------------------------


def test_outbox_row_exists_when_the_transaction_commits(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        loan_id = _make_loan(session)

    with sessions() as session:
        rows = _outbox(session)

    assert len(rows) == 1, "one posted transaction should queue exactly one event"
    assert rows[0].event_type == TRANSACTION_POSTED
    assert rows[0].aggregate_id == loan_id
    assert rows[0].published_at is None, "a fresh outbox row is unsent"


def test_a_rolled_back_post_publishes_nothing(sessions: sessionmaker[Session]) -> None:
    """The other half of 'iff': no commit, no event.

    The rollback is forced by an error raised after the posting, which is the
    realistic shape of this failure, rather than by never committing.
    """
    with pytest.raises(RuntimeError, match="forced"), sessions() as session, session.begin():
        _make_loan(session)
        raise RuntimeError("forced failure after the ledger entries were written")

    with sessions() as session:
        assert _outbox(session) == [], "a rolled back transaction must leave no event behind"
        entries = session.execute(
            select(func.count()).select_from(LedgerEntry.__table__)
        ).scalar_one()
        assert entries == 0, "and no ledger entries either"


def test_a_payment_queues_its_own_event(sessions: sessionmaker[Session]) -> None:
    with sessions() as session, session.begin():
        loan_id = _make_loan(session)
    with sessions() as session, session.begin():
        apply_payment(session, loan_id=loan_id, amount_cents=30_000)

    with sessions() as session:
        rows = _outbox(session)

    assert len(rows) == 2, "the disbursement and the payment are two posted transactions"
    payment_event = rows[-1]
    accounts = {posting["account"] for posting in payment_event.payload["postings"]}
    assert accounts == {LedgerAccount.CASH.value, LedgerAccount.LOAN_RECEIVABLE.value}


def test_an_unbalanced_post_is_refused_and_queues_nothing(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        loan_id = _make_loan(session)

    with pytest.raises(ledger.UnbalancedTransaction), sessions() as session, session.begin():
        ledger.post(
            session,
            loan_id=loan_id,
            postings=[ledger.Posting(LedgerAccount.CASH, EntryDirection.DEBIT, 100)],
        )

    with sessions() as session:
        assert len(_outbox(session)) == 1, "only the disbursement event should exist"


# --- the publisher -------------------------------------------------------


def test_the_publisher_sends_unsent_rows_and_marks_them(
    sessions: sessionmaker[Session], broker: InMemoryBroker
) -> None:
    with sessions() as session, session.begin():
        loan_id = _make_loan(session)
    with sessions() as session, session.begin():
        apply_payment(session, loan_id=loan_id, amount_cents=10_000)

    assert publisher.publish_pending(broker, sessions=sessions, queue=QUEUE) == 2
    assert broker.depth(QUEUE) == 2

    with sessions() as session:
        assert all(row.published_at is not None for row in _outbox(session))

    assert publisher.publish_pending(broker, sessions=sessions, queue=QUEUE) == 0, (
        "a sent row must not be sent again"
    )
    assert broker.depth(QUEUE) == 2


def test_killing_the_publisher_before_it_publishes_loses_nothing(
    sessions: sessionmaker[Session], broker: InMemoryBroker
) -> None:
    """The acceptance condition: a crash between commit and publish.

    The broker raises on the first attempt, which is indistinguishable from the
    process dying at that moment: the row was committed, nothing reached the
    broker, and the mark never happened. A restart must still find it.
    """

    class FailingBroker(InMemoryBroker):
        def publish(self, queue: str, message: Message) -> None:
            raise ConnectionError("broker unreachable")

    with sessions() as session, session.begin():
        _make_loan(session)

    with pytest.raises(ConnectionError):
        publisher.publish_pending(FailingBroker(), sessions=sessions, queue=QUEUE)

    with sessions() as session:
        rows = _outbox(session)
    assert len(rows) == 1
    assert rows[0].published_at is None, "a failed publish must leave the row unsent"

    # Restart, with a working broker.
    assert publisher.publish_pending(broker, sessions=sessions, queue=QUEUE) == 1
    assert broker.depth(QUEUE) == 1
    with sessions() as session:
        assert _outbox(session)[0].published_at is not None


def test_a_batch_that_fails_partway_republishes_rather_than_losing(
    sessions: sessionmaker[Session], broker: InMemoryBroker
) -> None:
    """The at least once seam, stated as a test.

    The broker accepts the first message and then fails, which rolls the whole
    batch back. The first message is already out there, but its row still reads
    unsent, so a restart sends it a second time. The point of the assertion is
    the direction of the failure: a duplicate, never a gap. This is the case
    that distinguishes committing after the broker accepted from committing
    before it, which is the only ordering in the publisher that matters.
    """

    class FailsAfterFirst(InMemoryBroker):
        def __init__(self) -> None:
            super().__init__()
            self.accepted = 0

        def publish(self, queue: str, message: Message) -> None:
            if self.accepted >= 1:
                raise ConnectionError("broker died partway through the batch")
            self.accepted += 1
            super().publish(queue, message)

    with sessions() as session, session.begin():
        loan_id = _make_loan(session)
    with sessions() as session, session.begin():
        apply_payment(session, loan_id=loan_id, amount_cents=10_000)

    flaky = FailsAfterFirst()
    with pytest.raises(ConnectionError):
        publisher.publish_pending(flaky, sessions=sessions, queue=QUEUE, limit=10)

    assert flaky.accepted == 1, "the broker took one message before failing"
    with sessions() as session:
        rows = _outbox(session)
    assert len(rows) == 2
    assert all(row.published_at is None for row in rows), (
        "the whole batch rolled back, so nothing is recorded as sent"
    )

    # Restart. Both rows are sent, so the first event is now delivered twice.
    assert publisher.publish_pending(broker, sessions=sessions, queue=QUEUE, limit=10) == 2
    assert broker.depth(QUEUE) == 2, "no event was lost"

    # And the consumer absorbs the duplicate, so the read model is still right.
    delivered = [message.message_id for message in flaky.messages(QUEUE)]
    redelivered = [message.message_id for message in broker.messages(QUEUE)]
    assert set(delivered) <= set(redelivered), "the already delivered event came round again"

    for message in flaky.messages(QUEUE):
        broker.publish(QUEUE, message)
    outcomes = _drain(broker, sessions)
    assert outcomes.count(Outcome.APPLIED) == 2, "each distinct event applied once"
    assert outcomes.count(Outcome.DUPLICATE) == 1, "and the duplicate changed nothing"

    totals = _totals(sessions)
    assert totals[LedgerAccount.LOAN_RECEIVABLE.value] == (120_000, 10_000)
    assert sum(debit - credit for debit, credit in totals.values()) == 0


def test_the_publisher_claims_each_row_once_across_two_passes(
    sessions: sessionmaker[Session], broker: InMemoryBroker
) -> None:
    with sessions() as session, session.begin():
        loan_id = _make_loan(session)
    for amount in (1_000, 2_000, 3_000):
        with sessions() as session, session.begin():
            apply_payment(session, loan_id=loan_id, amount_cents=amount)

    first = publisher.publish_pending(broker, sessions=sessions, queue=QUEUE, limit=2)
    second = publisher.publish_pending(broker, sessions=sessions, queue=QUEUE, limit=10)

    assert (first, second) == (2, 2), "four events, claimed two then two"
    ids = [message.message_id for message in broker.messages(QUEUE)]
    assert len(ids) == len(set(ids)) == 4, "no row published twice"


# --- the consumer --------------------------------------------------------


def _drain(
    broker: InMemoryBroker, sessions: sessionmaker[Session], **kwargs: object
) -> list[Outcome]:
    outcomes: list[Outcome] = []
    while (message := broker.receive(QUEUE)) is not None:
        handled = consumer.handle_once(
            broker,
            message,
            sessions=sessions,
            consumer=CONSUMER,
            queue=QUEUE,
            dead_letter_queue=DLQ,
            backoff_seconds=0.0,
            **kwargs,  # type: ignore[arg-type]
        )
        outcomes.append(handled.outcome)
    return outcomes


def _totals(sessions: sessionmaker[Session]) -> dict[str, tuple[int, int]]:
    with sessions() as session:
        return {
            row.account.value: (row.debit_cents, row.credit_cents)
            for row in session.execute(select(AccountTotal)).scalars()
        }


def test_the_consumer_builds_per_account_running_totals(
    sessions: sessionmaker[Session], broker: InMemoryBroker
) -> None:
    with sessions() as session, session.begin():
        loan_id = _make_loan(session)
    with sessions() as session, session.begin():
        apply_payment(session, loan_id=loan_id, amount_cents=30_000)

    publisher.publish_pending(broker, sessions=sessions, queue=QUEUE)
    assert _drain(broker, sessions) == [Outcome.APPLIED, Outcome.APPLIED]

    totals = _totals(sessions)
    # Disbursement: receivable debited 120000, funding payable credited 120000
    # (apr_bps is 0, so there is no interest leg). Payment: cash debited 30000,
    # receivable credited 30000.
    assert totals[LedgerAccount.LOAN_RECEIVABLE.value] == (120_000, 30_000)
    assert totals[LedgerAccount.FUNDING_PAYABLE.value] == (0, 120_000)
    assert totals[LedgerAccount.CASH.value] == (30_000, 0)

    net = sum(debit - credit for debit, credit in totals.values())
    assert net == 0, "the read model is built from balanced transactions, so it nets to zero"


def test_a_duplicate_delivery_is_a_no_op(
    sessions: sessionmaker[Session], broker: InMemoryBroker
) -> None:
    """The guarantee that makes at least once delivery safe."""
    with sessions() as session, session.begin():
        _make_loan(session)
    publisher.publish_pending(broker, sessions=sessions, queue=QUEUE)

    first = broker.receive(QUEUE)
    assert first is not None
    applied = consumer.handle_once(
        broker,
        first,
        sessions=sessions,
        consumer=CONSUMER,
        queue=QUEUE,
        dead_letter_queue=DLQ,
        backoff_seconds=0.0,
    )
    assert applied.outcome is Outcome.APPLIED
    before = _totals(sessions)

    # The same bytes again, as a broker redelivering would send them.
    broker.publish(QUEUE, Message(message_id=first.message_id, body=first.body))
    again = broker.receive(QUEUE)
    assert again is not None
    repeated = consumer.handle_once(
        broker,
        again,
        sessions=sessions,
        consumer=CONSUMER,
        queue=QUEUE,
        dead_letter_queue=DLQ,
        backoff_seconds=0.0,
    )

    assert repeated.outcome is Outcome.DUPLICATE
    assert _totals(sessions) == before, "a redelivery must not move the totals"
    assert broker.depth(DLQ) == 0, "a duplicate is not a failure"

    with sessions() as session:
        markers = session.execute(
            select(func.count()).select_from(ProcessedEvent.__table__)
        ).scalar_one()
    assert markers == 1, "one marker row per event, however many times it arrives"


def test_redelivery_after_an_unacked_crash_changes_nothing(
    sessions: sessionmaker[Session], broker: InMemoryBroker
) -> None:
    """A worker that dies holding a delivery, after its transaction committed.

    The broker returns the message; the consumer must absorb it.
    """
    with sessions() as session, session.begin():
        _make_loan(session)
    publisher.publish_pending(broker, sessions=sessions, queue=QUEUE)

    message = broker.receive(QUEUE)
    assert message is not None
    consumer.handle_event(decode(message.body), sessions=sessions, consumer=CONSUMER)
    before = _totals(sessions)

    # Never acked, so the broker gives it back.
    assert broker.requeue_unacked(QUEUE) == 1
    assert _drain(broker, sessions) == [Outcome.DUPLICATE]
    assert _totals(sessions) == before


def test_two_consumers_each_see_every_event(
    sessions: sessionmaker[Session], broker: InMemoryBroker
) -> None:
    """The marker is per consumer, so a second reader is not starved by the first."""
    with sessions() as session, session.begin():
        _make_loan(session)
    publisher.publish_pending(broker, sessions=sessions, queue=QUEUE)
    message = broker.receive(QUEUE)
    assert message is not None

    for name in ("reader-a", "reader-b"):
        handled = consumer.handle_once(
            broker,
            message,
            sessions=sessions,
            consumer=name,
            queue=QUEUE,
            dead_letter_queue=DLQ,
            backoff_seconds=0.0,
        )
        assert handled.outcome is Outcome.APPLIED, f"{name} should handle it on its own marker"


# --- the dead letter queue ----------------------------------------------


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("not json", b"{definitely not json"),
        ("wrong event type", encode(event_id=uuid.uuid4(), event_type="other.thing", payload={})),
        (
            "negative amount",
            encode(
                event_id=uuid.uuid4(),
                event_type=TRANSACTION_POSTED,
                payload={
                    "loan_id": str(uuid.uuid4()),
                    "transaction_id": str(uuid.uuid4()),
                    "occurred_at": "2026-01-15T00:00:00+00:00",
                    "postings": [{"account": "cash", "direction": "debit", "amount_cents": -5}],
                },
            ),
        ),
        (
            "unknown account",
            encode(
                event_id=uuid.uuid4(),
                event_type=TRANSACTION_POSTED,
                payload={
                    "loan_id": str(uuid.uuid4()),
                    "transaction_id": str(uuid.uuid4()),
                    "occurred_at": "2026-01-15T00:00:00+00:00",
                    "postings": [
                        {"account": "not_an_account", "direction": "debit", "amount_cents": 5}
                    ],
                },
            ),
        ),
    ],
)
def test_a_poison_message_reaches_the_dead_letter_queue(
    sessions: sessionmaker[Session], broker: InMemoryBroker, label: str, body: bytes
) -> None:
    broker.publish(QUEUE, Message(message_id=f"poison-{label}", body=body))
    message = broker.receive(QUEUE)
    assert message is not None

    handled = consumer.handle_once(
        broker,
        message,
        sessions=sessions,
        consumer=CONSUMER,
        queue=QUEUE,
        dead_letter_queue=DLQ,
        max_attempts=3,
        backoff_seconds=0.0,
    )

    assert handled.outcome is Outcome.DEAD_LETTERED, f"{label} should be dead lettered"
    assert handled.error
    assert broker.depth(DLQ) == 1
    assert broker.messages(DLQ)[0].body == body, "the dead letter carries the original bytes"
    assert broker.depth(QUEUE) == 0, "and it is not left blocking the queue"
    assert _totals(sessions) == {}, "a poison message must not touch the read model"


def test_the_consumer_retries_a_bounded_number_of_times(
    sessions: sessionmaker[Session], broker: InMemoryBroker
) -> None:
    """Bounded, and the bound is honoured exactly."""
    attempts: list[int] = []
    original = consumer.handle_event

    def counting(event: object, **kwargs: object) -> None:
        attempts.append(1)
        raise RuntimeError("transient failure")

    consumer.handle_event = counting  # type: ignore[assignment]
    try:
        with sessions() as session, session.begin():
            _make_loan(session)
        publisher.publish_pending(broker, sessions=sessions, queue=QUEUE)
        message = broker.receive(QUEUE)
        assert message is not None
        handled = consumer.handle_once(
            broker,
            message,
            sessions=sessions,
            consumer=CONSUMER,
            queue=QUEUE,
            dead_letter_queue=DLQ,
            max_attempts=4,
            backoff_seconds=0.0,
        )
    finally:
        consumer.handle_event = original  # type: ignore[assignment]

    assert len(attempts) == 4, "exactly max_attempts tries, no more and no fewer"
    assert handled.outcome is Outcome.DEAD_LETTERED
    assert broker.depth(DLQ) == 1


def test_a_transient_failure_that_clears_is_applied_not_dead_lettered(
    sessions: sessionmaker[Session], broker: InMemoryBroker
) -> None:
    calls: list[int] = []
    original = consumer.handle_event

    def flaky(event: object, **kwargs: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient failure")
        original(event, **kwargs)  # type: ignore[arg-type]

    consumer.handle_event = flaky  # type: ignore[assignment]
    try:
        with sessions() as session, session.begin():
            _make_loan(session)
        publisher.publish_pending(broker, sessions=sessions, queue=QUEUE)
        message = broker.receive(QUEUE)
        assert message is not None
        handled = consumer.handle_once(
            broker,
            message,
            sessions=sessions,
            consumer=CONSUMER,
            queue=QUEUE,
            dead_letter_queue=DLQ,
            max_attempts=3,
            backoff_seconds=0.0,
        )
    finally:
        consumer.handle_event = original  # type: ignore[assignment]

    assert len(calls) == 2, "failed once, then succeeded"
    assert handled.outcome is Outcome.APPLIED
    assert broker.depth(DLQ) == 0
    assert _totals(sessions)[LedgerAccount.LOAN_RECEIVABLE.value] == (120_000, 0)


# --- end to end ----------------------------------------------------------


def test_post_publish_consume_end_to_end_over_http(
    client: httpx.Client, sessions: sessionmaker[Session], broker: InMemoryBroker
) -> None:
    """Through the real API, so the outbox is written by the service's own path."""
    loan = http_create_loan(client)
    assert pay(client, loan["id"], 30_000).status_code == 201

    assert publisher.publish_pending(broker, sessions=sessions, queue=QUEUE) == 2
    assert _drain(broker, sessions) == [Outcome.APPLIED, Outcome.APPLIED]

    totals = _totals(sessions)
    assert totals[LedgerAccount.CASH.value] == (30_000, 0)
    assert sum(debit - credit for debit, credit in totals.values()) == 0
