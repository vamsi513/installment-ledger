"""The real RabbitMQ adapter, against a real broker.

These are the tests ``tests/test_events.py`` deliberately cannot be: everything
there runs against ``InMemoryBroker``, so the queue declarations, the dead
letter exchange, the delivery tags and RabbitMQ's own redelivery behaviour were
configured but never exercised. This module exercises them.

It is skipped, at module level and with a reason, unless a broker is actually
reachable. A developer without Docker running still gets a green suite, and CI
has the broker service so it runs there. The skip is deliberately not a silent
collection error: a run that does not cover this says so.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

BROKER_URL = os.environ.get("EVENTS_BROKER_URL", "amqp://guest:guest@localhost:5672/")
# Short, because the point of the probe is to decide quickly whether to skip.
PROBE_TIMEOUT_SECONDS = 3.0


def _unreachable() -> str | None:
    """Return why the broker cannot be used, or ``None`` if it can."""
    try:
        import pika
    except ImportError:
        return (
            "pika is not installed, though it is pinned in requirements.txt. "
            "Install it with: pip install -r requirements.txt"
        )

    try:
        parameters = pika.URLParameters(BROKER_URL)
        parameters.socket_timeout = PROBE_TIMEOUT_SECONDS
        parameters.blocked_connection_timeout = PROBE_TIMEOUT_SECONDS
        parameters.connection_attempts = 1
        parameters.retry_delay = 0
        pika.BlockingConnection(parameters).close()
    except Exception as exc:
        return (
            f"no broker reachable at {BROKER_URL} ({type(exc).__name__}). "
            "Start one with: docker compose up -d rabbitmq"
        )
    return None


UNREACHABLE = _unreachable()

if UNREACHABLE is not None:
    pytest.skip(
        f"RabbitMQ integration tests skipped: {UNREACHABLE}",
        allow_module_level=True,
    )

# Everything below is imported after the guard on purpose: with no broker and
# no pika, importing at the top of the file would be a collection error rather
# than a skip. Hence the E402 suppressions, which are not laziness.
from datetime import date  # noqa: E402

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.events import consumer, publisher  # noqa: E402
from app.events.broker import Message  # noqa: E402
from app.events.consumer import Outcome  # noqa: E402
from app.events.rabbit import QueueConfigurationConflict, RabbitBroker  # noqa: E402
from app.events.schema import decode  # noqa: E402
from app.models import AccountTotal, LedgerAccount  # noqa: E402
from app.services.loans import create_loan  # noqa: E402
from app.services.payments import apply_payment  # noqa: E402


@dataclass
class Target:
    """A broker plus a queue pair nothing else is using."""

    broker: RabbitBroker
    queue: str
    dlq: str


def _delete_queues(*names: str) -> None:
    import pika

    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    try:
        channel = connection.channel()
        for name in names:
            # Each delete gets its own channel: deleting a queue that does not
            # exist is not an error, but any other protocol error would close
            # the channel and take the rest of the loop with it.
            try:
                channel.queue_delete(queue=name)
            except Exception:
                channel = connection.channel()
    finally:
        connection.close()


@pytest.fixture
def target() -> Iterator[Target]:
    """A uniquely named queue pair, deleted afterwards.

    Unique names rather than a shared queue cleared between tests, so these
    tests cannot interfere with each other, with a developer's own broker, or
    with the compose stack's real queues.
    """
    suffix = uuid.uuid4().hex[:12]
    queue, dlq = f"test.{suffix}", f"test.{suffix}.dlq"
    broker = RabbitBroker(BROKER_URL, dead_letter_queue=dlq)
    try:
        yield Target(broker=broker, queue=queue, dlq=dlq)
    finally:
        broker.close()
        _delete_queues(queue, dlq)


def _drain(broker: RabbitBroker, queue: str, timeout_seconds: float = 2.0) -> list[Message]:
    received: list[Message] = []
    while (message := broker.receive(queue, timeout_seconds=timeout_seconds)) is not None:
        received.append(message)
        broker.ack(message)
        timeout_seconds = 0.2
    return received


# --- declaration ---------------------------------------------------------


def test_publishing_declares_the_queue_and_its_dead_letter_queue(target: Target) -> None:
    """Both queues exist, durable, after a single publish."""
    import pika

    target.broker.publish(target.queue, Message(message_id="d1", body=b"{}"))

    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    try:
        channel = connection.channel()
        # A passive declare asserts existence without creating anything.
        main = channel.queue_declare(queue=target.queue, passive=True)
        dead = channel.queue_declare(queue=target.dlq, passive=True)
    finally:
        connection.close()

    assert main.method.message_count == 1, "the published message is on the main queue"
    assert dead.method.message_count == 0, "and nothing is on the dead letter queue yet"


def test_the_queue_carries_the_dead_letter_arguments(target: Target) -> None:
    """Declared with the DLX, so RabbitMQ itself will route a rejection.

    Asserted by re-declaring with exactly the same arguments, which succeeds
    only if the existing queue already has them: a mismatch is a 406. That is
    a stronger check than reading the arguments back, because it is the same
    equivalence test the broker applies to the service on every startup.
    """
    import pika

    target.broker.publish(target.queue, Message(message_id="d2", body=b"{}"))

    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    try:
        channel = connection.channel()
        channel.queue_declare(
            queue=target.queue,
            durable=True,
            arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": target.dlq},
        )
    finally:
        connection.close()


def test_a_conflicting_queue_raises_and_leaves_the_broker_usable(target: Target) -> None:
    """Regression test for a bug the live broker found.

    A queue that already exists with different arguments makes the broker
    close the channel with a 406. The adapter used to let that escape as a
    bare ``ChannelClosedByBroker``, and because a closed channel fails every
    later call, the broker object stayed broken for the life of the process:
    the publisher loop would catch the error and spin forever publishing
    nothing. It now reports the conflict and reopens the channel.
    """
    import pika

    conflicting = f"{target.queue}.conflicting"
    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    try:
        connection.channel().queue_declare(queue=conflicting, durable=True)  # no DLX arguments
    finally:
        connection.close()

    try:
        with pytest.raises(QueueConfigurationConflict) as caught:
            target.broker.publish(conflicting, Message(message_id="c1", body=b"{}"))

        assert conflicting in str(caught.value)
        assert target.dlq in str(caught.value), "the message should name what it tried to declare"

        # The point of the fix: the broker still works afterwards.
        target.broker.publish(target.queue, Message(message_id="c2", body=b"after"))
        recovered = target.broker.receive(target.queue, timeout_seconds=2.0)
        assert recovered is not None, "the broker must still be usable after a declare conflict"
        assert recovered.body == b"after"
        target.broker.ack(recovered)
    finally:
        _delete_queues(conflicting)


# --- publish, consume, ack ----------------------------------------------


def test_publish_then_consume_round_trips_the_message(target: Target) -> None:
    body = b'{"event_type":"transaction.posted","n":1}'
    target.broker.publish(target.queue, Message(message_id="rt-1", body=body))

    received = target.broker.receive(target.queue, timeout_seconds=3.0)

    assert received is not None
    assert received.body == body, "the bytes survive the round trip unchanged"
    assert received.message_id == "rt-1", "and so does the message id"
    assert received.redelivered is False, "a first delivery is not a redelivery"
    assert isinstance(received.handle, int), "the delivery tag comes back for acking"
    target.broker.ack(received)


def test_receive_on_an_empty_queue_returns_none(target: Target) -> None:
    target.broker.publish(target.queue, Message(message_id="e1", body=b"{}"))
    assert _drain(target.broker, target.queue)

    assert target.broker.receive(target.queue, timeout_seconds=0.5) is None


def test_an_acked_message_is_not_redelivered(target: Target) -> None:
    """Ack really settles it, including across a reconnect."""
    target.broker.publish(target.queue, Message(message_id="a1", body=b"{}"))
    first = target.broker.receive(target.queue, timeout_seconds=3.0)
    assert first is not None
    target.broker.ack(first)

    # A new connection, so nothing is hidden in this client's local state.
    other = RabbitBroker(BROKER_URL, dead_letter_queue=target.dlq)
    try:
        assert other.receive(target.queue, timeout_seconds=1.0) is None
    finally:
        other.close()


def test_messages_come_back_in_the_order_they_were_published(target: Target) -> None:
    for index in range(5):
        target.broker.publish(
            target.queue, Message(message_id=f"o{index}", body=str(index).encode())
        )

    received = _drain(target.broker, target.queue, timeout_seconds=3.0)

    assert [message.message_id for message in received] == [f"o{i}" for i in range(5)]


def test_acking_a_message_with_no_delivery_tag_is_refused(target: Target) -> None:
    """A hand built message is not a delivery and cannot be settled."""
    with pytest.raises(ValueError, match="delivery tag"):
        target.broker.ack(Message(message_id="nope", body=b"{}"))


# --- the dead letter exchange -------------------------------------------


def test_a_rejected_message_reaches_the_dead_letter_queue(target: Target) -> None:
    """RabbitMQ's own dead lettering, which the queue declaration sets up.

    This is the route the consumer does *not* take — it dead letters by
    publishing and acking, because that works against the double too. This
    test is why the declaration is more than decoration.
    """
    body = b"poison"
    target.broker.publish(target.queue, Message(message_id="dl-1", body=body))
    delivery = target.broker.receive(target.queue, timeout_seconds=3.0)
    assert delivery is not None

    target.broker.reject(delivery)

    dead = target.broker.receive(target.dlq, timeout_seconds=3.0)
    assert dead is not None, "the broker should have moved it to the dead letter queue"
    assert dead.body == body, "the dead lettered message keeps its original bytes"
    assert dead.message_id == "dl-1"
    target.broker.ack(dead)

    assert target.broker.receive(target.queue, timeout_seconds=0.5) is None, (
        "and it is gone from the queue it was blocking"
    )


def test_a_rejected_message_with_requeue_comes_back_marked_redelivered(target: Target) -> None:
    target.broker.publish(target.queue, Message(message_id="rq-1", body=b"{}"))
    first = target.broker.receive(target.queue, timeout_seconds=3.0)
    assert first is not None
    assert first.redelivered is False

    target.broker.reject(first, requeue=True)

    again = target.broker.receive(target.queue, timeout_seconds=3.0)
    assert again is not None
    assert again.message_id == "rq-1"
    assert again.redelivered is True, "the broker marks a second delivery as a redelivery"
    target.broker.ack(again)
    assert target.broker.receive(target.dlq, timeout_seconds=0.5) is None, (
        "requeue is not dead lettering"
    )


# --- redelivery after a consumer disconnects ----------------------------


def test_an_unacked_message_is_redelivered_after_the_consumer_disconnects(
    target: Target,
) -> None:
    """The failure the at-least-once guarantee rests on.

    A worker that dies holding a delivery must not take the message with it.
    ``tests/test_events.py`` models this with ``requeue_unacked`` on the
    double; here the real broker does it because the connection went away.
    """
    target.broker.publish(target.queue, Message(message_id="u1", body=b"unacked"))

    dying = RabbitBroker(BROKER_URL, dead_letter_queue=target.dlq)
    held = dying.receive(target.queue, timeout_seconds=3.0)
    assert held is not None
    assert held.message_id == "u1"
    dying.close()  # never acked

    redelivered = target.broker.receive(target.queue, timeout_seconds=5.0)

    assert redelivered is not None, "an unacked message must come back, not vanish"
    assert redelivered.body == b"unacked"
    assert redelivered.redelivered is True, "and the broker says it is a redelivery"
    target.broker.ack(redelivered)


# --- the real publisher and consumer, over the real broker --------------


def test_the_publisher_and_consumer_work_over_a_real_broker(target: Target, engine: object) -> None:
    """End to end on real infrastructure: Postgres, RabbitMQ, and both workers.

    Everything else in this module tests the adapter directly. This one runs
    the production ``publish_pending`` and ``handle_once`` against it, which is
    what proves ``RabbitBroker`` actually satisfies the interface those two
    rely on rather than merely resembling it.
    """
    sessions = sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    with engine.begin() as connection:  # type: ignore[attr-defined]
        connection.execute(text("TRUNCATE outbox_events, processed_events, account_totals CASCADE"))

    consumer_name = f"rabbit-it-{uuid.uuid4().hex[:8]}"

    with sessions() as session, session.begin():
        view = create_loan(
            session,
            principal_cents=120_000,
            apr_bps=0,
            installment_count=4,
            cadence="biweekly",
            start_date=date(2026, 1, 15),
        )
        loan_id = view.loan.id
    with sessions() as session, session.begin():
        apply_payment(session, loan_id=loan_id, amount_cents=30_000)

    published = publisher.publish_pending(
        target.broker, sessions=sessions, queue=target.queue, limit=10
    )
    assert published == 2, "the disbursement and the payment"

    outcomes: list[Outcome] = []
    for _ in range(2):
        message = target.broker.receive(target.queue, timeout_seconds=5.0)
        assert message is not None, "the publisher's messages should be on the real queue"
        handled = consumer.handle_once(
            target.broker,
            message,
            sessions=sessions,
            consumer=consumer_name,
            queue=target.queue,
            dead_letter_queue=target.dlq,
            backoff_seconds=0.0,
        )
        outcomes.append(handled.outcome)

    assert outcomes == [Outcome.APPLIED, Outcome.APPLIED]

    with sessions() as session:
        totals = {
            row.account.value: (row.debit_cents, row.credit_cents)
            for row in session.execute(select(AccountTotal)).scalars()
        }
    assert totals[LedgerAccount.LOAN_RECEIVABLE.value] == (120_000, 30_000)
    assert totals[LedgerAccount.CASH.value] == (30_000, 0)
    assert sum(debit - credit for debit, credit in totals.values()) == 0

    assert target.broker.receive(target.dlq, timeout_seconds=0.5) is None, "nothing dead lettered"


def test_a_redelivery_over_the_real_broker_changes_nothing(target: Target, engine: object) -> None:
    """The duplicate case, with the broker genuinely redelivering it.

    ``tests/test_events.py`` proves the consumer absorbs a duplicate using the
    double. Here the message comes back because a real connection dropped it
    unacked, so the whole path is real.
    """
    sessions = sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    with engine.begin() as connection:  # type: ignore[attr-defined]
        connection.execute(text("TRUNCATE outbox_events, processed_events, account_totals CASCADE"))

    consumer_name = f"rabbit-dup-{uuid.uuid4().hex[:8]}"

    with sessions() as session, session.begin():
        create_loan(
            session,
            principal_cents=120_000,
            apr_bps=0,
            installment_count=4,
            cadence="biweekly",
            start_date=date(2026, 1, 15),
        )

    publisher.publish_pending(target.broker, sessions=sessions, queue=target.queue, limit=10)

    # A worker takes it, commits its transaction, then dies before acking.
    dying = RabbitBroker(BROKER_URL, dead_letter_queue=target.dlq)
    first = dying.receive(target.queue, timeout_seconds=5.0)
    assert first is not None
    consumer.handle_event(decode(first.body), sessions=sessions, consumer=consumer_name)
    dying.close()

    with sessions() as session:
        before = {
            row.account.value: (row.debit_cents, row.credit_cents)
            for row in session.execute(select(AccountTotal)).scalars()
        }
    assert before, "the first handling did move the totals"

    # The real broker gives it back.
    redelivered = target.broker.receive(target.queue, timeout_seconds=5.0)
    assert redelivered is not None
    assert redelivered.redelivered is True

    handled = consumer.handle_once(
        target.broker,
        redelivered,
        sessions=sessions,
        consumer=consumer_name,
        queue=target.queue,
        dead_letter_queue=target.dlq,
        backoff_seconds=0.0,
    )

    assert handled.outcome is Outcome.DUPLICATE
    with sessions() as session:
        after = {
            row.account.value: (row.debit_cents, row.credit_cents)
            for row in session.execute(select(AccountTotal)).scalars()
        }
    assert after == before, "a real redelivery must leave the read model untouched"
