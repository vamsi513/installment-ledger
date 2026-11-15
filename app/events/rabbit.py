"""The RabbitMQ adapter.

RabbitMQ rather than Kafka for three reasons that matter at this size: the dead
letter queue this design needs is a native concept here and has to be built by
hand on Kafka; ``pika`` is pure Python with no transitive dependencies, so it
drops into a fully pinned lock without a compiled wheel; and one broker
container is a smaller operational commitment than a Kafka cluster for a low
volume stream of business events. Nothing here needs a partitioned log, and the
consumer is idempotent, so per key ordering buys nothing either.

``pika`` is imported inside the constructor, not at module scope, so importing
this module costs nothing and needs nothing. The ledger and
``tests/test_events.py`` never construct this class and so never load pika;
``tests/test_rabbit_integration.py`` does, against a real broker, and skips
itself when there is not one.
"""

from __future__ import annotations

import contextlib
from typing import Any

from . import config
from .broker import Message

# Persist messages to disk, so a broker restart does not drop what it holds.
_PERSISTENT_DELIVERY_MODE = 2

# AMQP's reply code for declaring a queue whose arguments do not match the one
# the broker already has.
_PRECONDITION_FAILED = 406


class QueueConfigurationConflict(RuntimeError):
    """The broker already holds this queue, declared with different arguments.

    Raised instead of letting a bare AMQP 406 escape, because the fix is an
    operational one and the error should say so: either delete the existing
    queue or point this service at a different name. A queue created by an
    earlier deploy, or by hand, is the usual cause.
    """


class RabbitBroker:
    """A ``Broker`` backed by a real RabbitMQ connection.

    Queues are declared durable and the main queue is declared with the dead
    letter queue as its dead letter target, so there are two routes to it: the
    broker's own, taken by ``reject``, and an explicit republish, which is what
    the consumer uses because it behaves the same against the in memory double.
    Both are covered by ``tests/test_rabbit_integration.py``.
    """

    def __init__(self, url: str | None = None, *, dead_letter_queue: str | None = None) -> None:
        try:
            import pika
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "RabbitBroker needs the 'pika' package. It is pinned in "
                "requirements.txt; install it, or use InMemoryBroker."
            ) from exc

        self._pika = pika
        self._dlq = dead_letter_queue or config.dead_letter_queue_name()
        self._connection = pika.BlockingConnection(pika.URLParameters(url or config.broker_url()))
        self._declared: set[str] = set()
        self._open_channel()

    def _open_channel(self) -> None:
        """Open a fresh channel and forget what was declared on the old one.

        Any protocol error closes the channel, and a closed channel fails every
        later call with ``ChannelWrongStateError``. Without this the first bad
        declare would brick the object for the life of the process: the worker
        loops catch the exception and carry on, so a publisher would spin
        forever publishing nothing.
        """
        self._channel = self._connection.channel()
        self._declared.clear()
        # No effect on basic_get, which is what receive() uses: prefetch bounds
        # what the broker pushes to a basic_consume subscriber, and basic_get
        # pulls one message per call regardless. Kept because it is correct for
        # a consume based receive, and harmless now.
        self._channel.basic_qos(prefetch_count=1)

    def _declare(self, queue: str) -> None:
        if queue in self._declared:
            return
        try:
            if queue == self._dlq:
                self._channel.queue_declare(queue=queue, durable=True)
            else:
                self._channel.queue_declare(
                    queue=queue,
                    durable=True,
                    arguments={
                        "x-dead-letter-exchange": "",
                        "x-dead-letter-routing-key": self._dlq,
                    },
                )
                self._channel.queue_declare(queue=self._dlq, durable=True)
                self._declared.add(self._dlq)
        except self._pika.exceptions.ChannelClosedByBroker as exc:
            # The broker closed the channel as it raised. Get a working one
            # back before reporting, so a caller that handles the error can
            # keep using this broker.
            self._open_channel()
            if exc.reply_code == _PRECONDITION_FAILED:
                raise QueueConfigurationConflict(
                    f"the broker already has a queue named {queue!r} with different "
                    f"arguments, so it cannot be declared with "
                    f"x-dead-letter-routing-key={self._dlq!r}. Delete that queue or "
                    f"set EVENTS_QUEUE to a different name. Broker said: {exc.reply_text}"
                ) from exc
            raise
        self._declared.add(queue)

    def publish(self, queue: str, message: Message) -> None:
        self._declare(queue)
        properties: Any = self._pika.BasicProperties(
            message_id=message.message_id,
            delivery_mode=_PERSISTENT_DELIVERY_MODE,
            content_type="application/json",
        )
        self._channel.basic_publish(
            exchange="", routing_key=queue, body=message.body, properties=properties
        )

    def receive(self, queue: str, timeout_seconds: float = 0.0) -> Message | None:
        self._declare(queue)
        method, properties, body = self._channel.basic_get(queue=queue, auto_ack=False)
        if method is None:
            if timeout_seconds > 0:
                # Let pika service the connection rather than sleeping on it.
                self._connection.process_data_events(time_limit=timeout_seconds)
                method, properties, body = self._channel.basic_get(queue=queue, auto_ack=False)
            if method is None:
                return None
        message_id = getattr(properties, "message_id", None) or ""
        return Message(
            message_id=message_id,
            body=body,
            handle=method.delivery_tag,
            redelivered=bool(getattr(method, "redelivered", False)),
        )

    def _delivery_tag(self, message: Message) -> int:
        # ``handle`` is typed ``object`` on the interface, because each adapter
        # puts something different in it. Narrow it here rather than casting:
        # a message from a different broker, or one built by hand in a test,
        # should say so plainly instead of failing inside pika.
        tag = message.handle
        if not isinstance(tag, int):
            raise ValueError(
                f"this message carries no usable delivery tag (got {type(tag).__name__}) "
                "and cannot be settled"
            )
        return tag

    def ack(self, message: Message) -> None:
        self._channel.basic_ack(delivery_tag=self._delivery_tag(message))

    def reject(self, message: Message, *, requeue: bool = False) -> None:
        """Refuse a delivery, by default routing it to the dead letter queue.

        This is RabbitMQ's own dead lettering: with ``requeue=False`` the broker
        moves the message to the ``x-dead-letter-exchange`` the queue was
        declared with. It is deliberately not on the ``Broker`` protocol. The
        consumer dead letters by publishing to the DLQ and acking, because that
        path behaves the same against the in memory double, and a protocol
        carrying a RabbitMQ specific concept would have to be emulated by every
        other adapter. This method exists so the route the queue declaration
        sets up is reachable and testable rather than merely configured.

        ``requeue=True`` puts the message back for another delivery instead.
        """
        self._channel.basic_reject(delivery_tag=self._delivery_tag(message), requeue=requeue)

    def close(self) -> None:
        # A connection that is already gone is the normal case on shutdown, and
        # there is nothing useful to do about it.
        with contextlib.suppress(Exception):  # pragma: no cover - closing a dead connection
            self._connection.close()
