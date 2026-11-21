"""The consent decision log.

One row per consent decision: the mode, what the rule said, what the classifier
said, whether they agreed, which of them decided, and why if the classifier was
not used. Append only, and enforced by SQLite triggers rather than by
convention, in the same spirit as ``assistant.review.audit`` and the ledger's
own balance trigger.

WHY A SEPARATE TABLE AND NOT ``review_audit_log``. Three reasons, in order of
how much they matter:

1. Different process. The review audit log is written by the MCP server, which
   owns the pending action store. The consent decision is made in the agent
   process, before any tool is called. Writing the review log from the agent
   would put a second writer on a table whose whole point is a single ordered
   history of review transitions.
2. Different subject. ``review_audit_log`` records what happened to a pending
   action, keyed on its status before and after, with a closed set of actions in
   ``AuditAction``. A consent decision changes no action status. Recording it
   there would mean a new action value that no status transition corresponds to,
   and squeezing a label, a confidence, a threshold and an agreement flag into
   the free text ``reason`` column, where nothing could query them.
3. Different question. Shadow mode exists to be counted: how often do the two
   decisions agree, and on what. That is a query over typed columns.

The reply text is stored in full. That is a deliberate choice and it mirrors the
reason the review audit log stores loan ids in full while the log stream
redacts them: a consent trail that cannot show what was taken as consent is not
a consent trail. The consequence is that this database holds user text, so it
belongs wherever the review audit database belongs and not in a repository.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from enum import StrEnum


class DecidedBy(StrEnum):
    """Which path actually produced the outcome."""

    #: The existing confirmation rule, because the mode is rule or shadow.
    RULE = "rule"
    #: The classifier, through the threshold.
    CLASSIFIER = "classifier"
    #: The existing confirmation rule, because the classifier could not be used.
    RULE_FALLBACK = "rule_fallback"


class ConsentAction(StrEnum):
    """What the gate decided to do about the proposal."""

    ACT = "act"
    CANCEL = "cancel"
    REPROPOSE = "repropose"
    ASK_AGAIN = "ask_again"


class ConsentFailure(StrEnum):
    """Why the classifier was not used. Absent when it was."""

    TIMEOUT = "timeout"
    MODEL_ERROR = "model_error"
    INVALID_OUTPUT = "invalid_output"


_TABLE = """
CREATE TABLE IF NOT EXISTS consent_decisions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at           REAL NOT NULL,
    session_id            TEXT,
    pending_action_id     TEXT,
    loan_id               TEXT,
    amount_cents          INTEGER,
    mode                  TEXT NOT NULL,
    reply_text            TEXT,
    rule_confirm          INTEGER NOT NULL,
    classifier_label      TEXT,
    classifier_confidence REAL,
    modified_amount_cents INTEGER,
    threshold             REAL NOT NULL,
    decided_by            TEXT NOT NULL,
    action                TEXT NOT NULL,
    agreed                INTEGER,
    failure               TEXT,
    latency_ms            INTEGER
);
CREATE INDEX IF NOT EXISTS consent_decisions_action_id
    ON consent_decisions (pending_action_id, id);
CREATE INDEX IF NOT EXISTS consent_decisions_mode
    ON consent_decisions (mode, id);
"""

_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS consent_decisions_no_update
BEFORE UPDATE ON consent_decisions
BEGIN
    SELECT RAISE(ABORT, 'consent_decisions is append only');
END;

CREATE TRIGGER IF NOT EXISTS consent_decisions_no_delete
BEFORE DELETE ON consent_decisions
BEGIN
    SELECT RAISE(ABORT, 'consent_decisions is append only');
END;
"""


@dataclass(frozen=True)
class ConsentRecord:
    """One logged decision, as it comes back out."""

    id: int
    recorded_at: float
    session_id: str | None
    pending_action_id: str | None
    loan_id: str | None
    amount_cents: int | None
    mode: str
    reply_text: str | None
    rule_confirm: bool
    classifier_label: str | None
    classifier_confidence: float | None
    modified_amount_cents: int | None
    threshold: float
    decided_by: str
    action: str
    agreed: bool | None
    failure: str | None
    latency_ms: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "recorded_at": self.recorded_at,
            "session_id": self.session_id,
            "pending_action_id": self.pending_action_id,
            "loan_id": self.loan_id,
            "amount_cents": self.amount_cents,
            "mode": self.mode,
            "rule_confirm": self.rule_confirm,
            "classifier_label": self.classifier_label,
            "classifier_confidence": self.classifier_confidence,
            "modified_amount_cents": self.modified_amount_cents,
            "threshold": self.threshold,
            "decided_by": self.decided_by,
            "action": self.action,
            "agreed": self.agreed,
            "failure": self.failure,
            "latency_ms": self.latency_ms,
        }


def _row_to_record(row: sqlite3.Row) -> ConsentRecord:
    agreed = row["agreed"]
    return ConsentRecord(
        id=int(row["id"]),
        recorded_at=float(row["recorded_at"]),
        session_id=row["session_id"],
        pending_action_id=row["pending_action_id"],
        loan_id=row["loan_id"],
        amount_cents=row["amount_cents"],
        mode=row["mode"],
        reply_text=row["reply_text"],
        rule_confirm=bool(row["rule_confirm"]),
        classifier_label=row["classifier_label"],
        classifier_confidence=row["classifier_confidence"],
        modified_amount_cents=row["modified_amount_cents"],
        threshold=float(row["threshold"]),
        decided_by=row["decided_by"],
        action=row["action"],
        agreed=None if agreed is None else bool(agreed),
        failure=row["failure"],
        latency_ms=row["latency_ms"],
    )


class ConsentLog:
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
        mode: str,
        rule_confirm: bool,
        threshold: float,
        decided_by: DecidedBy,
        action: ConsentAction,
        session_id: str | None = None,
        pending_action_id: str | None = None,
        loan_id: str | None = None,
        amount_cents: int | None = None,
        reply_text: str | None = None,
        classifier_label: str | None = None,
        classifier_confidence: float | None = None,
        modified_amount_cents: int | None = None,
        agreed: bool | None = None,
        failure: ConsentFailure | None = None,
        latency_ms: int | None = None,
    ) -> ConsentRecord:
        recorded_at = time.time()
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                """
                INSERT INTO consent_decisions (
                    recorded_at, session_id, pending_action_id, loan_id, amount_cents,
                    mode, reply_text, rule_confirm, classifier_label,
                    classifier_confidence, modified_amount_cents, threshold,
                    decided_by, action, agreed, failure, latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recorded_at,
                    session_id,
                    pending_action_id,
                    loan_id,
                    amount_cents,
                    mode,
                    reply_text,
                    int(rule_confirm),
                    classifier_label,
                    classifier_confidence,
                    modified_amount_cents,
                    threshold,
                    decided_by.value,
                    action.value,
                    None if agreed is None else int(agreed),
                    None if failure is None else failure.value,
                    latency_ms,
                ),
            )
            row_id = int(cursor.lastrowid or 0)
            cursor.execute("SELECT * FROM consent_decisions WHERE id = ?", (row_id,))
            row = cursor.fetchone()
        if row is None:  # pragma: no cover - the insert above just succeeded
            raise RuntimeError("the consent decision row disappeared after being written")
        return _row_to_record(row)

    def for_action(self, pending_action_id: str) -> list[ConsentRecord]:
        """Every decision recorded against one pending action, oldest first."""
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                "SELECT * FROM consent_decisions WHERE pending_action_id = ? ORDER BY id",
                (pending_action_id,),
            )
            return [_row_to_record(row) for row in cursor.fetchall()]

    def recent(self, limit: int = 50) -> list[ConsentRecord]:
        """The most recent decisions, newest first."""
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute("SELECT * FROM consent_decisions ORDER BY id DESC LIMIT ?", (limit,))
            return [_row_to_record(row) for row in cursor.fetchall()]

    def agreement_counts(self) -> dict[str, int]:
        """How often the two decisions agreed, for shadow mode.

        Rows where the classifier was not used carry a NULL ``agreed`` and are
        counted separately rather than silently as agreement.
        """
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                """
                SELECT
                    SUM(CASE WHEN agreed = 1 THEN 1 ELSE 0 END) AS agreed,
                    SUM(CASE WHEN agreed = 0 THEN 1 ELSE 0 END) AS disagreed,
                    SUM(CASE WHEN agreed IS NULL THEN 1 ELSE 0 END) AS not_compared,
                    COUNT(*) AS total
                FROM consent_decisions
                """
            )
            row = cursor.fetchone()
        return {
            "agreed": int(row["agreed"] or 0),
            "disagreed": int(row["disagreed"] or 0),
            "not_compared": int(row["not_compared"] or 0),
            "total": int(row["total"] or 0),
        }
