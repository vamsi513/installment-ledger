"""The review policy and the audit log. No LLM, no network, database required."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from assistant.pending import ActionStatus, PendingActionStore
from assistant.review.audit import AuditAction, AuditLog
from assistant.review.policy import (
    AMOUNT_ABOVE_THRESHOLD,
    TOO_MANY_PREPARES,
    ReviewPolicy,
)

OFF = ReviewPolicy(enabled=False, threshold_cents=100, max_prepares=1, window_seconds=60)
ON = ReviewPolicy(enabled=True, threshold_cents=40_000, max_prepares=3, window_seconds=3_600)


def test_the_policy_holds_nothing_when_it_is_off() -> None:
    """The default. Existing behaviour has to be untouched."""
    decision = OFF.decide(amount_cents=10_000_000, earlier_prepares_in_window=99)
    assert decision.needs_review is False
    assert decision.reasons == ()


@pytest.mark.parametrize("amount", [1, 100, 39_999, 40_000])
def test_an_amount_at_or_under_the_threshold_passes(amount: int) -> None:
    assert ON.decide(amount_cents=amount, earlier_prepares_in_window=0).needs_review is False


@pytest.mark.parametrize("amount", [40_001, 50_000, 1_000_000])
def test_an_amount_above_the_threshold_is_held(amount: int) -> None:
    decision = ON.decide(amount_cents=amount, earlier_prepares_in_window=0)
    assert decision.needs_review is True
    assert decision.reasons == (AMOUNT_ABOVE_THRESHOLD,)


@pytest.mark.parametrize("earlier", [0, 1, 2])
def test_under_the_prepare_limit_passes(earlier: int) -> None:
    assert ON.decide(amount_cents=100, earlier_prepares_in_window=earlier).needs_review is False


@pytest.mark.parametrize("earlier", [3, 4, 20])
def test_more_prepares_than_the_limit_is_held(earlier: int) -> None:
    """Three already prepared means this one is the fourth, which is more than three."""
    decision = ON.decide(amount_cents=100, earlier_prepares_in_window=earlier)
    assert decision.needs_review is True
    assert decision.reasons == (TOO_MANY_PREPARES,)


def test_both_rules_can_fire_at_once() -> None:
    decision = ON.decide(amount_cents=99_000, earlier_prepares_in_window=5)
    assert decision.needs_review is True
    assert decision.reasons == (AMOUNT_ABOVE_THRESHOLD, TOO_MANY_PREPARES)
    assert decision.reason_text() == "amount_above_threshold, too_many_prepares_in_window"


def test_a_zero_threshold_switches_the_amount_rule_off() -> None:
    policy = ReviewPolicy(enabled=True, threshold_cents=0, max_prepares=3, window_seconds=60)
    assert (
        policy.decide(amount_cents=10_000_000, earlier_prepares_in_window=0).needs_review is False
    )


def test_a_zero_prepare_limit_switches_the_frequency_rule_off() -> None:
    policy = ReviewPolicy(enabled=True, threshold_cents=40_000, max_prepares=0, window_seconds=60)
    assert policy.decide(amount_cents=100, earlier_prepares_in_window=999).needs_review is False


def test_the_policy_reads_itself_from_settings(review_settings) -> None:
    policy = ReviewPolicy.from_settings(review_settings)
    assert policy.enabled is True
    assert policy.threshold_cents == review_settings.review_threshold_cents
    assert policy.max_prepares == review_settings.review_max_prepares
    assert policy.window_seconds == review_settings.review_window_seconds


def test_settings_default_to_the_queue_being_off(assistant_settings) -> None:
    assert assistant_settings.review_enabled is False
    assert ReviewPolicy.from_settings(assistant_settings).enabled is False


# ----------------------------------------------------------------- audit log


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(str(tmp_path / "audit.sqlite3"))


def test_an_entry_records_who_what_when_and_the_transition(audit: AuditLog) -> None:
    event = audit.record(
        actor="reviewer-1",
        action=AuditAction.APPROVED,
        pending_action_id="p1",
        loan_id="11111111-1111-4111-8111-111111111111",
        amount_cents=99_000,
        old_status="needs_review",
        new_status="approved",
        reason="checked with the borrower",
    )
    assert event.id == 1
    assert event.recorded_at > 0
    stored = audit.entries(pending_action_id="p1")
    assert len(stored) == 1
    assert stored[0].as_dict() == event.as_dict()
    assert stored[0].actor == "reviewer-1"
    assert stored[0].old_status == "needs_review"
    assert stored[0].new_status == "approved"
    assert stored[0].reason == "checked with the borrower"


def test_entries_come_back_in_the_order_they_were_written(audit: AuditLog) -> None:
    for action in (AuditAction.PREPARED, AuditAction.HELD_FOR_REVIEW, AuditAction.APPROVED):
        audit.record(actor="a", action=action, pending_action_id="p1")
    assert [entry.action for entry in audit.entries(pending_action_id="p1")] == [
        AuditAction.PREPARED,
        AuditAction.HELD_FOR_REVIEW,
        AuditAction.APPROVED,
    ]


def test_entries_are_filtered_by_action(audit: AuditLog) -> None:
    audit.record(actor="a", action=AuditAction.APPROVED, pending_action_id="p1")
    audit.record(actor="a", action=AuditAction.APPROVED, pending_action_id="p2")
    assert len(audit.entries(pending_action_id="p1")) == 1
    assert audit.count() == 2


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE review_audit_log SET reason = 'tampered' WHERE id = 1",
        "UPDATE review_audit_log SET actor = 'somebody else'",
        "UPDATE review_audit_log SET new_status = 'approved'",
        "DELETE FROM review_audit_log WHERE id = 1",
        "DELETE FROM review_audit_log",
    ],
)
def test_the_database_refuses_to_change_or_remove_an_entry(tmp_path: Path, statement: str) -> None:
    """Append only is enforced by triggers, not by this package being careful."""
    path = str(tmp_path / "audit.sqlite3")
    log = AuditLog(path)
    log.record(
        actor="reviewer-1",
        action=AuditAction.APPROVED,
        pending_action_id="p1",
        reason="original",
    )

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append only"):
            connection.execute(statement)
            connection.commit()
    finally:
        connection.close()

    assert log.count() == 1
    assert log.entries()[0].reason == "original"


def test_a_prepared_action_survives_reopening_the_log(tmp_path: Path) -> None:
    path = str(tmp_path / "audit.sqlite3")
    AuditLog(path).record(actor="a", action=AuditAction.PREPARED, pending_action_id="p1")
    assert AuditLog(path).count() == 1


# --------------------------------------------------- store status transitions


@pytest.fixture
def store(tmp_path: Path) -> PendingActionStore:
    return PendingActionStore(str(tmp_path / "pending.sqlite3"), ttl_seconds=300)


def test_a_new_action_is_ready_by_default(store: PendingActionStore) -> None:
    action = store.create(loan_id="L1", amount_cents=100)
    assert action.status is ActionStatus.READY
    assert action.is_confirmable is True


def test_a_held_action_is_not_confirmable(store: PendingActionStore) -> None:
    action = store.create(loan_id="L1", amount_cents=100, status=ActionStatus.NEEDS_REVIEW)
    assert store.get(action.pending_action_id).is_confirmable is False


def test_only_the_first_transition_takes_effect(store: PendingActionStore) -> None:
    action = store.create(loan_id="L1", amount_cents=100, status=ActionStatus.NEEDS_REVIEW)
    first = store.transition(
        action.pending_action_id, expected=ActionStatus.NEEDS_REVIEW, new=ActionStatus.APPROVED
    )
    second = store.transition(
        action.pending_action_id, expected=ActionStatus.NEEDS_REVIEW, new=ActionStatus.APPROVED
    )
    assert (first, second) == (True, False)
    assert store.get(action.pending_action_id).status is ActionStatus.APPROVED


def test_approving_cannot_follow_a_rejection(store: PendingActionStore) -> None:
    action = store.create(loan_id="L1", amount_cents=100, status=ActionStatus.NEEDS_REVIEW)
    store.transition(
        action.pending_action_id, expected=ActionStatus.NEEDS_REVIEW, new=ActionStatus.REJECTED
    )
    assert (
        store.transition(
            action.pending_action_id,
            expected=ActionStatus.NEEDS_REVIEW,
            new=ActionStatus.APPROVED,
        )
        is False
    )
    assert store.get(action.pending_action_id).status is ActionStatus.REJECTED


def test_approval_does_not_change_the_idempotency_key(store: PendingActionStore) -> None:
    """The key is what makes a payment happen once, so review must not touch it."""
    action = store.create(loan_id="L1", amount_cents=100, status=ActionStatus.NEEDS_REVIEW)
    before = action.idempotency_key
    store.transition(
        action.pending_action_id, expected=ActionStatus.NEEDS_REVIEW, new=ActionStatus.APPROVED
    )
    assert store.get(action.pending_action_id).idempotency_key == before


def test_only_the_first_consumption_is_reported(store: PendingActionStore) -> None:
    action = store.create(loan_id="L1", amount_cents=100)
    assert store.mark_consumed(action.pending_action_id) is True
    assert store.mark_consumed(action.pending_action_id) is False


def test_prepares_are_counted_per_loan_and_window(store: PendingActionStore) -> None:
    for _ in range(3):
        store.create(loan_id="L1", amount_cents=100)
    store.create(loan_id="L2", amount_cents=100)
    assert store.count_prepares_since("L1", since=0) == 3
    assert store.count_prepares_since("L2", since=0) == 1
    assert store.count_prepares_since("L1", since=2_000_000_000) == 0


def test_held_actions_are_listed_oldest_first(store: PendingActionStore) -> None:
    held = [
        store.create(loan_id="L1", amount_cents=amount, status=ActionStatus.NEEDS_REVIEW)
        for amount in (100, 200, 300)
    ]
    store.create(loan_id="L1", amount_cents=400)
    listed = store.list_by_status(ActionStatus.NEEDS_REVIEW)
    assert [item.amount_cents for item in listed] == [100, 200, 300]
    assert [item.pending_action_id for item in listed] == [item.pending_action_id for item in held]
