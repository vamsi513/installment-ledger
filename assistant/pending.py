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
from enum import StrEnum

IDEMPOTENCY_KEY_PREFIX = "assistant-pending"


class ActionStatus(StrEnum):
    """Where a prepared payment stands with respect to review.

    ``READY`` is what every action is when the review queue is off, and is the
    only status the assistant produced before the queue existed.
    """

    READY = "ready"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


#: Statuses ``confirm_payment`` will post for. Everything else it refuses.
CONFIRMABLE_STATUSES = frozenset({ActionStatus.READY, ActionStatus.APPROVED})

# Tables first, then any column a file written by an earlier version is missing,
# then the indexes. The index on ``status`` cannot be created before the column
# exists, which is exactly the case on an old state file.
_TABLES = """
CREATE TABLE IF NOT EXISTS pending_actions (
    pending_action_id TEXT PRIMARY KEY,
    loan_id           TEXT    NOT NULL,
    amount_cents      INTEGER NOT NULL,
    created_at        REAL    NOT NULL,
    expires_at        REAL    NOT NULL,
    consumed_at       REAL,
    status            TEXT    NOT NULL DEFAULT 'ready'
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS pending_actions_loan_created
    ON pending_actions (loan_id, created_at);
CREATE INDEX IF NOT EXISTS pending_actions_status
    ON pending_actions (status);
"""


@dataclass(frozen=True)
class PendingAction:
    pending_action_id: str
    loan_id: str
    amount_cents: int
    created_at: float
    expires_at: float
    consumed_at: float | None
    status: ActionStatus = ActionStatus.READY

    def is_expired(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    @property
    def is_confirmable(self) -> bool:
        """True when the review queue has nothing more to say about this action."""
        return self.status in CONFIRMABLE_STATUSES

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
            connection.executescript(_TABLES)
            self._add_missing_columns(connection)
            connection.executescript(_INDEXES)

    @staticmethod
    def _add_missing_columns(connection: sqlite3.Connection) -> None:
        """Bring a file written before the review queue existed up to date.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so a state
        file from an earlier version would otherwise be missing ``status``.
        """
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(pending_actions)").fetchall()
        }
        if "status" not in columns:
            connection.execute(
                "ALTER TABLE pending_actions ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'"
            )

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

    def create(
        self,
        *,
        loan_id: str,
        amount_cents: int,
        status: ActionStatus = ActionStatus.READY,
    ) -> PendingAction:
        now = time.time()
        action = PendingAction(
            pending_action_id=uuid.uuid4().hex,
            loan_id=str(loan_id),
            amount_cents=int(amount_cents),
            created_at=now,
            expires_at=now + self._ttl_seconds,
            consumed_at=None,
            status=status,
        )
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                "INSERT INTO pending_actions "
                "(pending_action_id, loan_id, amount_cents, created_at, expires_at, "
                "consumed_at, status) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (
                    action.pending_action_id,
                    action.loan_id,
                    action.amount_cents,
                    action.created_at,
                    action.expires_at,
                    action.status.value,
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
        return _row_to_action(row)

    def require_live(self, pending_action_id: str) -> PendingAction:
        """Fetch an action, refusing an expired one."""
        action = self.get(pending_action_id)
        if action.is_expired():
            raise PendingActionExpired(pending_action_id)
        return action

    def mark_consumed(self, pending_action_id: str) -> bool:
        """Record the first confirmation. Later confirmations leave it alone.

        Returns True only for the call that actually set it, so a caller can
        tell a first confirmation from a replay of one.
        """
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                "UPDATE pending_actions SET consumed_at = ? "
                "WHERE pending_action_id = ? AND consumed_at IS NULL",
                (time.time(), pending_action_id),
            )
            return cursor.rowcount == 1

    def count_prepares_since(self, loan_id: str, *, since: float) -> int:
        """How many actions this loan has had prepared since a point in time."""
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM pending_actions WHERE loan_id = ? AND created_at >= ?",
                (str(loan_id), since),
            )
            return int(cursor.fetchone()[0])

    def list_by_status(self, status: ActionStatus, *, limit: int = 100) -> list[PendingAction]:
        """Actions in one status, oldest first."""
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                "SELECT * FROM pending_actions WHERE status = ? ORDER BY created_at, "
                "pending_action_id LIMIT ?",
                (status.value, int(limit)),
            )
            return [_row_to_action(row) for row in cursor.fetchall()]

    def transition(
        self, pending_action_id: str, *, expected: ActionStatus, new: ActionStatus
    ) -> bool:
        """Move an action between statuses, once.

        The ``WHERE status = expected`` is what makes a second approval a no op
        rather than a second transition: SQLite applies the update to one row or
        to none, and the row count says which happened. Two racing approvals
        therefore produce exactly one transition and one audit entry.
        """
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                "UPDATE pending_actions SET status = ? WHERE pending_action_id = ? AND status = ?",
                (new.value, pending_action_id, expected.value),
            )
            return cursor.rowcount == 1


def _row_to_action(row: sqlite3.Row) -> PendingAction:
    keys = row.keys()
    raw_status = row["status"] if "status" in keys and row["status"] is not None else "ready"
    return PendingAction(
        pending_action_id=row["pending_action_id"],
        loan_id=row["loan_id"],
        amount_cents=int(row["amount_cents"]),
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        consumed_at=None if row["consumed_at"] is None else float(row["consumed_at"]),
        status=ActionStatus(raw_status),
    )
