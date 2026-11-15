"""The broker seam.

Everything above this module talks to ``Broker``, never to a client library, so
the publisher and the consumer are the same code whether they are driven by
RabbitMQ or by the in memory double the tests use.

The interface is pull based with an explicit ack because that is the behaviour
the delivery guarantee depends on: a message that is received and not acked is
redelivered. ``InMemoryBroker.requeue_unacked`` models exactly that, so a worker
crash is something the tests can reproduce rather than describe.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Message:
    """One message in flight.

    ``handle`` is the adapter's private means of acking this delivery: a
    delivery tag for RabbitMQ, the message itself for the double. It is
    excluded from equality and from the repr so two messages compare by their
    id and bytes, which is what a test wants to assert on.
    """

    message_id: str
    body: bytes
    handle: object | None = field(default=None, compare=False, repr=False)


class Broker(Protocol):
    def publish(self, queue: str, message: Message) -> None:
        """Hand a message to ``queue``. Returns once the broker has accepted it."""

    def receive(self, queue: str, timeout_seconds: float = 0.0) -> Message | None:
        """Take one message, or return ``None`` if none arrived in time."""

    def ack(self, message: Message) -> None:
        """Confirm the message was handled, so it is not redelivered."""

    def close(self) -> None:
        """Release whatever the adapter holds."""


class InMemoryBroker:
    """A broker that keeps queues in a dict. The test double.

    It is deliberately not a simulation of RabbitMQ. It models only the two
    behaviours the code above it depends on, which are that a published message
    can be received once and that an unacked message can come back.
    """

    def __init__(self) -> None:
        self._queues: dict[str, deque[Message]] = {}
        self._unacked: dict[str, list[Message]] = {}
        self._lock = threading.Lock()

    def publish(self, queue: str, message: Message) -> None:
        with self._lock:
            self._queues.setdefault(queue, deque()).append(message)

    def receive(self, queue: str, timeout_seconds: float = 0.0) -> Message | None:
        with self._lock:
            pending = self._queues.get(queue)
            if not pending:
                return None
            message = pending.popleft()
            self._unacked.setdefault(queue, []).append(message)
            return message

    def ack(self, message: Message) -> None:
        with self._lock:
            for held in self._unacked.values():
                for index, candidate in enumerate(held):
                    if candidate is message:
                        held.pop(index)
                        return

    def close(self) -> None:
        return None

    # -- test affordances, not part of the Broker interface ----------------

    def depth(self, queue: str) -> int:
        """How many messages are waiting on ``queue``, not counting unacked ones."""
        with self._lock:
            return len(self._queues.get(queue, ()))

    def messages(self, queue: str) -> list[Message]:
        with self._lock:
            return list(self._queues.get(queue, ()))

    def unacked(self, queue: str) -> list[Message]:
        with self._lock:
            return list(self._unacked.get(queue, ()))

    def requeue_unacked(self, queue: str) -> int:
        """Return every unacked message to the front of the queue.

        What a broker does when a consumer dies holding deliveries.
        """
        with self._lock:
            held = self._unacked.pop(queue, [])
            pending = self._queues.setdefault(queue, deque())
            for message in reversed(held):
                pending.appendleft(message)
            return len(held)
