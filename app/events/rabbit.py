"""The RabbitMQ adapter.

RabbitMQ rather than Kafka for three reasons that matter at this size: the dead
letter queue this design needs is a native concept here and has to be built by
hand on Kafka; ``pika`` is pure Python with no transitive dependencies, so it
drops into a fully pinned lock without a compiled wheel; and one broker
container is a smaller operational commitment than a Kafka cluster for a low
volume stream of business events. Nothing here needs a partitioned log, and the
consumer is idempotent, so per key ordering buys nothing either.

``pika`` is imported inside the constructor, not at module scope. The tests and
the ledger itself never construct this class, so neither has to have the
dependency installed to import the modules around it.
"""

from __future__ import annotations

import contextlib
from typing import Any

from . import config
from .broker import Message

# Persist messages to disk, so a broker restart does not drop what it holds.
_PERSISTENT_DELIVERY_MODE = 2


class RabbitBroker:
    """A ``Broker`` backed by a real RabbitMQ connection.

    Queues are declared durable and the main queue is declared with the dead
    letter queue as its dead letter target. The consumer also republishes to
    the dead letter queue explicitly once it gives up on a message, which is
    what the tests assert on, because that path works identically against the
    in memory double.
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
        self._channel = self._connection.channel()
        self._declared: set[str] = set()
        # One unacked message at a time, so a second worker gets the next one
        # rather than this one sitting in a local buffer.
        self._channel.basic_qos(prefetch_count=1)

    def _declare(self, queue: str) -> None:
        if queue in self._declared:
            return
        if queue == self._dlq:
            self._channel.queue_declare(queue=queue, durable=True)
        else:
            self._channel.queue_declare(
                queue=queue,
                durable=True,
                arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": self._dlq},
            )
            self._channel.queue_declare(queue=self._dlq, durable=True)
            self._declared.add(self._dlq)
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
        return Message(message_id=message_id, body=body, handle=method.delivery_tag)

    def ack(self, message: Message) -> None:
        # ``handle`` is typed ``object`` on the interface, because each adapter
        # puts something different in it. Narrow it here rather than casting:
        # a message from a different broker, or one built by hand in a test,
        # should say so plainly instead of failing inside pika.
        tag = message.handle
        if not isinstance(tag, int):
            raise ValueError(
                f"this message carries no usable delivery tag (got {type(tag).__name__}) "
                "and cannot be acked"
            )
        self._channel.basic_ack(delivery_tag=tag)

    def close(self) -> None:
        # A connection that is already gone is the normal case on shutdown, and
        # there is nothing useful to do about it.
        with contextlib.suppress(Exception):  # pragma: no cover - closing a dead connection
            self._connection.close()
