"""Idempotent handling of POST requests.

The guarantee comes from a unique index, not from an application level check.
The key row is inserted in the same transaction as the write it guards:

* first request inserts the key, does the work, stores its response, commits;
* a concurrent request with the same key blocks on the unique index during its
  own insert, and when the first commits it gets a unique violation, rolls
  back, and reads the stored response instead of writing again;
* if the first request fails, its key row rolls back with it, so the key is not
  burned by a failed attempt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import new_session
from .errors import IdempotencyKeyConflict, IdempotencyRequestInFlight
from .models import IdempotencyKey

JsonResponse = tuple[int, dict[str, Any]]


class _KeyAlreadyClaimed(Exception):
    """Internal signal that the unique index rejected our insert."""


def request_hash(endpoint: str, payload: dict[str, Any]) -> str:
    """A stable hash of the request, so a replay with a changed body is detected."""
    canonical = json.dumps(
        {"endpoint": endpoint, "payload": payload}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_once(
    *,
    key: str,
    endpoint: str,
    payload: dict[str, Any],
    work: Callable[[Session], JsonResponse],
) -> JsonResponse:
    """Run ``work`` at most once for this key, or replay its stored response."""
    digest = request_hash(endpoint, payload)

    try:
        with new_session() as session, session.begin():
            record = IdempotencyKey(key=key, endpoint=endpoint, request_hash=digest)
            session.add(record)
            try:
                session.flush()
            except IntegrityError as exc:
                raise _KeyAlreadyClaimed from exc

            status, body = work(session)
            record.response_status = status
            record.response_body = body
            return status, body
    except _KeyAlreadyClaimed:
        pass

    return _replay(key=key, digest=digest)


def _replay(*, key: str, digest: str) -> JsonResponse:
    """Return the response the original request stored under this key."""
    with new_session() as session:
        record = session.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == key)
        ).scalar_one()

        if record.request_hash != digest:
            raise IdempotencyKeyConflict(
                "This Idempotency-Key was already used with a different request body.",
                idempotency_key=key,
            )
        if record.response_body is None or record.response_status is None:
            raise IdempotencyRequestInFlight(
                "A request with this Idempotency-Key is still in progress.",
                idempotency_key=key,
            )
        return record.response_status, record.response_body
