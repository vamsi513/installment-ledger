"""Which loans a session is allowed to touch.

A session is bound to loan ids explicitly, and the agent refuses to read or pay
anything outside that set. The binding is stored rather than passed on every
turn so that a later turn carrying only a session id and a message is still
constrained to the same loans.

This is not authentication. It is the narrower thing the assistant needs: a
session cannot reach a loan nobody attached to it. Deciding who may open a
session at all is the caller's job, and the ledger has no auth to delegate to.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_loans (
    session_id TEXT NOT NULL,
    loan_id    TEXT NOT NULL,
    bound_at   REAL NOT NULL,
    PRIMARY KEY (session_id, loan_id)
);
"""

MAX_LOANS_PER_SESSION = 20


class TooManyLoansForSession(ValueError):
    """A session cannot be bound to more loans than the cap allows."""


def is_loan_id(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


class SessionLoanStore:
    """A tiny SQLite table. One connection per operation keeps it thread safe."""

    def __init__(self, path: str) -> None:
        self._path = path
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

    def bind(self, session_id: str, loan_ids: list[str]) -> list[str]:
        """Attach loans to a session and return everything the session now holds."""
        wanted = [str(loan_id).lower() for loan_id in loan_ids if is_loan_id(loan_id)]
        if wanted:
            existing = set(self.loans_for(session_id))
            if len(existing | set(wanted)) > MAX_LOANS_PER_SESSION:
                raise TooManyLoansForSession(
                    f"a session may hold at most {MAX_LOANS_PER_SESSION} loans"
                )
            now = time.time()
            with self._connect() as connection, closing(connection.cursor()) as cursor:
                cursor.executemany(
                    "INSERT OR IGNORE INTO session_loans (session_id, loan_id, bound_at) "
                    "VALUES (?, ?, ?)",
                    [(session_id, loan_id, now) for loan_id in wanted],
                )
        return self.loans_for(session_id)

    def loans_for(self, session_id: str) -> list[str]:
        with self._connect() as connection, closing(connection.cursor()) as cursor:
            cursor.execute(
                "SELECT loan_id FROM session_loans WHERE session_id = ? ORDER BY bound_at, loan_id",
                (session_id,),
            )
            return [row["loan_id"] for row in cursor.fetchall()]
