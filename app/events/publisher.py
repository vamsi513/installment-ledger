"""The publisher: unsent outbox rows out to the broker.

A batch is one transaction. Within it the publisher claims rows with
``FOR UPDATE SKIP LOCKED``, so a second publisher takes different rows rather
than waiting on these; publishes each one; marks each sent; and commits.

The guarantee is not about the order of the publish and the mark inside that
transaction, because a rollback undoes the mark whichever order they were
written in. It is about the commit: **a row is recorded as sent only by a
transaction that commits after the broker has already accepted the message.**
Three failures follow from that, and all of them fall towards duplication
rather than loss:

* died before publishing: the row is still unsent, and the next pass sends it;
* died after the broker accepted but before the commit: the message is out
  there, the row still reads unsent, and the next pass sends it again;
* the broker failed partway through a batch: the rows it had already accepted
  roll back unsent and are sent again.

So an event is never lost, and may arrive more than once. That is the whole of
why the consumer has to be idempotent, and it is the reason this service claims
at least once delivery and not exactly once.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..db import get_sessionmaker
from ..models import OutboxEvent
from . import config
from .broker import Broker, Message
from .schema import encode

logger = logging.getLogger("app.events.publisher")


def claim_unpublished(session: Session, *, limit: int) -> list[OutboxEvent]:
    """Lock and return the oldest unsent rows.

    ``SKIP LOCKED`` is what makes more than one publisher safe: a row another
    publisher is working on is passed over rather than waited for.
    """
    return list(
        session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
    )


def publish_pending(
    broker: Broker,
    *,
    sessions: sessionmaker[Session] | None = None,
    queue: str | None = None,
    limit: int | None = None,
) -> int:
    """Publish one batch of unsent events. Returns how many were published.

    Zero means there was nothing to do, which is the signal the loop uses to
    decide whether to sleep.
    """
    sessions = sessions or get_sessionmaker()
    queue = queue or config.queue_name()
    limit = limit or config.batch_size()

    with sessions() as session, session.begin():
        rows = claim_unpublished(session, limit=limit)
        for row in rows:
            broker.publish(
                queue,
                Message(
                    message_id=str(row.event_id),
                    body=encode(
                        event_id=row.event_id, event_type=row.event_type, payload=row.payload
                    ),
                ),
            )
            row.attempts += 1
            row.published_at = datetime.now(UTC)
        return len(rows)


def run(
    broker: Broker,
    *,
    sessions: sessionmaker[Session] | None = None,
    queue: str | None = None,
    stop: threading.Event | None = None,
    poll_seconds: float | None = None,
) -> None:
    """Publish until ``stop`` is set.

    An exception from one pass is logged and the loop continues, because the
    rows it failed on are still unsent and the next pass will find them.
    """
    stop = stop or threading.Event()
    interval = config.poll_seconds() if poll_seconds is None else poll_seconds

    while not stop.is_set():
        try:
            published = publish_pending(broker, sessions=sessions, queue=queue)
        except Exception:
            logger.exception("publish pass failed; the rows remain unsent and will be retried")
            published = 0
        if published == 0:
            stop.wait(interval)


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
    logger.info("publisher started on queue %s", config.queue_name())
    try:
        run(broker, stop=stop)
    finally:
        broker.close()
    logger.info("publisher stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
