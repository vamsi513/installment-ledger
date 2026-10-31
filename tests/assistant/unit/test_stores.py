"""The pending action and session loan stores. No LLM, no network, database required.

"Database required" is the ledger's Postgres, which the repository's autouse
fixture truncates before every test. These two stores are SQLite files in a
temporary directory and touch Postgres not at all.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from assistant.pending import (
    IDEMPOTENCY_KEY_PREFIX,
    PendingActionExpired,
    PendingActionNotFound,
    PendingActionStore,
)
from assistant.sessions import MAX_LOANS_PER_SESSION, SessionLoanStore, TooManyLoansForSession

LOAN_A = "11111111-1111-4111-8111-111111111111"
LOAN_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def pending_store(tmp_path: Path) -> PendingActionStore:
    return PendingActionStore(str(tmp_path / "pending.sqlite3"), ttl_seconds=300)


@pytest.fixture
def session_store(tmp_path: Path) -> SessionLoanStore:
    return SessionLoanStore(str(tmp_path / "sessions.sqlite3"))


def test_a_pending_action_round_trips(pending_store: PendingActionStore) -> None:
    created = pending_store.create(loan_id=LOAN_A, amount_cents=4_500)
    fetched = pending_store.get(created.pending_action_id)
    assert fetched.loan_id == LOAN_A
    assert fetched.amount_cents == 4_500
    assert fetched.consumed_at is None
    assert fetched.is_expired() is False


def test_the_idempotency_key_comes_from_the_pending_action_id_alone(
    pending_store: PendingActionStore,
) -> None:
    """This is what makes a double confirm, or a retry after a timeout, apply once."""
    created = pending_store.create(loan_id=LOAN_A, amount_cents=4_500)
    assert created.idempotency_key == f"{IDEMPOTENCY_KEY_PREFIX}-{created.pending_action_id}"
    assert pending_store.get(created.pending_action_id).idempotency_key == created.idempotency_key


def test_two_prepared_payments_get_different_keys(pending_store: PendingActionStore) -> None:
    first = pending_store.create(loan_id=LOAN_A, amount_cents=4_500)
    second = pending_store.create(loan_id=LOAN_A, amount_cents=4_500)
    assert first.idempotency_key != second.idempotency_key


def test_an_unknown_pending_action_is_not_found(pending_store: PendingActionStore) -> None:
    with pytest.raises(PendingActionNotFound):
        pending_store.get("nope")


def test_an_expired_pending_action_is_refused(tmp_path: Path) -> None:
    store = PendingActionStore(str(tmp_path / "short.sqlite3"), ttl_seconds=0)
    created = store.create(loan_id=LOAN_A, amount_cents=100)
    time.sleep(0.01)
    assert created.is_expired() is True
    with pytest.raises(PendingActionExpired):
        store.require_live(created.pending_action_id)
    # It is still readable, so a confirmation can be told why it failed.
    assert store.get(created.pending_action_id).amount_cents == 100


def test_marking_consumed_records_only_the_first_use(pending_store: PendingActionStore) -> None:
    created = pending_store.create(loan_id=LOAN_A, amount_cents=100)
    pending_store.mark_consumed(created.pending_action_id)
    first = pending_store.get(created.pending_action_id).consumed_at
    assert first is not None
    time.sleep(0.01)
    pending_store.mark_consumed(created.pending_action_id)
    assert pending_store.get(created.pending_action_id).consumed_at == first


def test_a_pending_action_survives_a_new_store_on_the_same_file(tmp_path: Path) -> None:
    """The tool server is stateless across processes because of this."""
    path = str(tmp_path / "shared.sqlite3")
    created = PendingActionStore(path).create(loan_id=LOAN_A, amount_cents=777)
    reopened = PendingActionStore(path).get(created.pending_action_id)
    assert reopened.amount_cents == 777
    assert reopened.idempotency_key == created.idempotency_key


def test_binding_loans_to_a_session(session_store: SessionLoanStore) -> None:
    assert session_store.bind("s1", [LOAN_A]) == [LOAN_A]
    assert session_store.bind("s1", [LOAN_B]) == [LOAN_A, LOAN_B]
    assert session_store.bind("s1", []) == [LOAN_A, LOAN_B]
    assert session_store.loans_for("s2") == []


def test_binding_the_same_loan_twice_is_harmless(session_store: SessionLoanStore) -> None:
    session_store.bind("s1", [LOAN_A])
    assert session_store.bind("s1", [LOAN_A, LOAN_A]) == [LOAN_A]


def test_sessions_do_not_see_each_others_loans(session_store: SessionLoanStore) -> None:
    session_store.bind("s1", [LOAN_A])
    session_store.bind("s2", [LOAN_B])
    assert session_store.loans_for("s1") == [LOAN_A]
    assert session_store.loans_for("s2") == [LOAN_B]


def test_anything_that_is_not_a_loan_id_is_dropped(session_store: SessionLoanStore) -> None:
    assert session_store.bind("s1", ["not-a-uuid", "", LOAN_A]) == [LOAN_A]


def test_a_session_cannot_hold_more_loans_than_the_cap(session_store: SessionLoanStore) -> None:
    many = [f"{index:08d}-1111-4111-8111-111111111111" for index in range(MAX_LOANS_PER_SESSION)]
    session_store.bind("s1", many)
    with pytest.raises(TooManyLoansForSession):
        session_store.bind("s1", [LOAN_A])
