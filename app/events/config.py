"""Runtime configuration for the publisher and the consumer.

Read from the environment, like ``app.config``, with defaults that point at the
compose broker. Nothing here is a secret and nothing has a committed value that
matters.
"""

from __future__ import annotations

import os

DEFAULT_BROKER_URL = "amqp://guest:guest@localhost:5672/"
DEFAULT_QUEUE = "ledger.transaction.posted"
DEFAULT_DEAD_LETTER_QUEUE = "ledger.transaction.posted.dlq"
DEFAULT_CONSUMER_NAME = "account-totals"


def broker_url() -> str:
    """AMQP URL for RabbitMQ. Ignored when the in memory broker is used."""
    return os.environ.get("EVENTS_BROKER_URL", DEFAULT_BROKER_URL)


def queue_name() -> str:
    return os.environ.get("EVENTS_QUEUE", DEFAULT_QUEUE)


def dead_letter_queue_name() -> str:
    return os.environ.get("EVENTS_DEAD_LETTER_QUEUE", DEFAULT_DEAD_LETTER_QUEUE)


def consumer_name() -> str:
    """Identifies the consumer in ``processed_events``.

    Two consumers with different names each process every event once; two
    processes sharing a name cooperate and process it once between them.
    """
    return os.environ.get("EVENTS_CONSUMER_NAME", DEFAULT_CONSUMER_NAME)


def max_attempts() -> int:
    """How many times the consumer handles a message before dead lettering it."""
    return max(1, int(os.environ.get("EVENTS_MAX_ATTEMPTS", "3")))


def batch_size() -> int:
    """How many unsent outbox rows the publisher claims per transaction."""
    return max(1, int(os.environ.get("EVENTS_BATCH_SIZE", "100")))


def poll_seconds() -> float:
    """How long to wait before looking for work again when there was none."""
    return max(0.0, float(os.environ.get("EVENTS_POLL_SECONDS", "1.0")))


def retry_backoff_seconds() -> float:
    """Pause between a failed handling attempt and the next one."""
    return max(0.0, float(os.environ.get("EVENTS_RETRY_BACKOFF_SECONDS", "0.1")))
