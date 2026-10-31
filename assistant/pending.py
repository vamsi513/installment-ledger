"""The pending action store.

``prepare_payment`` writes a row here and ``confirm_payment`` reads it. Keeping
it in SQLite rather than in the tool server's memory means the tool server is
stateless across processes, so a confirmation still resolves after a restart and
the same row is visible to every worker.

Rows are not deleted when they are used. The ledger is the thing that makes a
payment happen once, keyed on an ``Idempotency-Key`` derived from the pending
action id, so a retry after a timeout must be able to find the row again and
send the identical request. ``consumed_at`` records the first use for
observability only; it does not gate anything.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass

IDEMPOTENCY_KEY_PREFIX = "assistant-pending"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
    pending_action_id TEXT PRIMARY KEY,
    loan_id           TEXT    NOT NULL,
    amount_cents      INTEGER NOT NULL,
    created_at        REAL    NOT NULL,
    expires_at        REAL    NOT NULL,
    consumed_at       REAL
);
"""


@dataclass(frozen=True)
class PendingAction:
    pending_action_id: str
    loan_id: str
    amount_cents: int
    created_at: float
    expires_at: float
    consumed_at: float | None

    def is_expired(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    @property
    def idempotency_key(self) -> str:
        """The ledger key this action maps to.

        Derived from the pending action id alone, so confirming twice, or
        retrying after a timeout, sends the same key and the ledger applies the
        payment once.
        """
        return f"{IDEMPOTENCY_KEY_PREFIX}-{self.pending_action_id}"


class PendingActionExpired(LookupError):
    """The pending action exists but its expiry has passed."""


class PendingActionNotFound(LookupError):
    """No pending action with that id."""


class PendingActionStore:
    """A tiny SQLite table. One connection per operation keeps it thread safe."""

    def __init__(self, path: str, *, ttl_seconds: int = 300) -> None:
        self._path = path
        self._ttl_seconds = ttl_seconds
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=10.0, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=10000")
            yield connection
        finally:
            connection.close()

    def create(self, *, loan_id: str, amount_cents: int) -> PendingAction:
        now = time.time()
        action = PendingAction(
            pending_action_id=uuid.uuid4().hex,
            loan_id=str(loan_id),
            amount_cents=int(amount_cents),
            created_at=now,
            expires_at=now + self._ttl_seconds,
            consumed_at=None,
        )
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                "INSERT INTO pending_actions "
                "(pending_action_id, loan_id, amount_cents, created_at, expires_at, consumed_at) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    action.pending_action_id,
                    action.loan_id,
                    action.amount_cents,
                    action.created_at,
                    action.expires_at,
                ),
            )
        return action

    def get(self, pending_action_id: str) -> PendingAction:
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                "SELECT * FROM pending_actions WHERE pending_action_id = ?",
                (pending_action_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise PendingActionNotFound(pending_action_id)
        return PendingAction(
            pending_action_id=row["pending_action_id"],
            loan_id=row["loan_id"],
            amount_cents=int(row["amount_cents"]),
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
            consumed_at=None if row["consumed_at"] is None else float(row["consumed_at"]),
        )

    def require_live(self, pending_action_id: str) -> PendingAction:
        """Fetch an action, refusing an expired one."""
        action = self.get(pending_action_id)
        if action.is_expired():
            raise PendingActionExpired(pending_action_id)
        return action

    def mark_consumed(self, pending_action_id: str) -> None:
        """Record the first confirmation. Later confirmations leave it alone."""
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                "UPDATE pending_actions SET consumed_at = ? "
                "WHERE pending_action_id = ? AND consumed_at IS NULL",
                (time.time(), pending_action_id),
            )
