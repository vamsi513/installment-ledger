"""The audit log.

One row per state change: who, what, when, the status before, the status after,
and the reason. Append only, and not as a convention: SQLite triggers reject an
UPDATE or a DELETE on the table, in the same spirit as the ledger's balance
trigger. A bug in this package cannot quietly rewrite history.

Loan ids are stored in full here, unlike in the logs, because an audit trail
that cannot identify what was approved is not an audit trail. The redaction in
``assistant.redaction`` is for the log stream, which is a different audience.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from enum import StrEnum


class AuditAction(StrEnum):
    """What happened. Past tense, because an entry records something that did."""

    PREPARED = "prepared"
    HELD_FOR_REVIEW = "held_for_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONFIRM_REFUSED = "confirm_refused"
    CONFIRMED = "confirmed"
    CONFIRMED_REPLAY = "confirmed_replay"


#: The actor recorded when the assistant itself made the change rather than a person.
SYSTEM_ACTOR = "assistant"

_TABLE = """
CREATE TABLE IF NOT EXISTS review_audit_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at       REAL NOT NULL,
    actor             TEXT NOT NULL,
    action            TEXT NOT NULL,
    pending_action_id TEXT,
    loan_id           TEXT,
    amount_cents      INTEGER,
    old_status        TEXT,
    new_status        TEXT,
    reason            TEXT
);
CREATE INDEX IF NOT EXISTS review_audit_log_action_id
    ON review_audit_log (pending_action_id, id);
"""

# Append only, enforced by the database rather than by convention.
_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS review_audit_log_no_update
BEFORE UPDATE ON review_audit_log
BEGIN
    SELECT RAISE(ABORT, 'review_audit_log is append only');
END;

CREATE TRIGGER IF NOT EXISTS review_audit_log_no_delete
BEFORE DELETE ON review_audit_log
BEGIN
    SELECT RAISE(ABORT, 'review_audit_log is append only');
END;
"""


@dataclass(frozen=True)
class AuditEvent:
    id: int
    recorded_at: float
    actor: str
    action: AuditAction
    pending_action_id: str | None
    loan_id: str | None
    amount_cents: int | None
    old_status: str | None
    new_status: str | None
    reason: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "recorded_at": self.recorded_at,
            "actor": self.actor,
            "action": self.action.value,
            "pending_action_id": self.pending_action_id,
            "loan_id": self.loan_id,
            "amount_cents": self.amount_cents,
            "old_status": self.old_status,
            "new_status": self.new_status,
            "reason": self.reason,
        }


class AuditLog:
    """A tiny append only SQLite table. One connection per operation."""

    def __init__(self, path: str) -> None:
        self._path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_TABLE)
            connection.executescript(_TRIGGERS)

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

    def record(
        self,
        *,
        actor: str,
        action: AuditAction,
        pending_action_id: str | None = None,
        loan_id: str | None = None,
        amount_cents: int | None = None,
        old_status: str | None = None,
        new_status: str | None = None,
        reason: str | None = None,
    ) -> AuditEvent:
        recorded_at = time.time()
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                "INSERT INTO review_audit_log "
                "(recorded_at, actor, action, pending_action_id, loan_id, amount_cents, "
                "old_status, new_status, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    recorded_at,
                    actor,
                    action.value,
                    pending_action_id,
                    loan_id,
                    None if amount_cents is None else int(amount_cents),
                    old_status,
                    new_status,
                    reason,
                ),
            )
            entry_id = int(cursor.lastrowid or 0)
        return AuditEvent(
            id=entry_id,
            recorded_at=recorded_at,
            actor=actor,
            action=action,
            pending_action_id=pending_action_id,
            loan_id=loan_id,
            amount_cents=None if amount_cents is None else int(amount_cents),
            old_status=old_status,
            new_status=new_status,
            reason=reason,
        )

    def entries(
        self, *, pending_action_id: str | None = None, limit: int = 200
    ) -> list[AuditEvent]:
        """Entries in the order they were written, oldest first."""
        query = "SELECT * FROM review_audit_log"
        parameters: tuple[object, ...] = ()
        if pending_action_id is not None:
            query += " WHERE pending_action_id = ?"
            parameters = (pending_action_id,)
        query += " ORDER BY id LIMIT ?"
        parameters = (*parameters, int(limit))

        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(query, parameters)
            return [_row_to_event(row) for row in cursor.fetchall()]

    def count(self) -> int:
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute("SELECT COUNT(*) FROM review_audit_log")
            return int(cursor.fetchone()[0])


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        id=int(row["id"]),
        recorded_at=float(row["recorded_at"]),
        actor=str(row["actor"]),
        action=AuditAction(row["action"]),
        pending_action_id=row["pending_action_id"],
        loan_id=row["loan_id"],
        amount_cents=None if row["amount_cents"] is None else int(row["amount_cents"]),
        old_status=row["old_status"],
        new_status=row["new_status"],
        reason=row["reason"],
    )
