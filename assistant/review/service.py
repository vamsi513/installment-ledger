"""The review queue's operations.

Everything that changes an action's status goes through here, so there is one
place that writes the audit log and one place that decides whether a transition
is legal.

The at most once guarantee is not weakened by any of this. The ledger still
decides whether a payment happened, keyed on an ``Idempotency-Key`` derived from
the pending action id, and that id does not change when an action is approved.
Review only decides whether ``confirm_payment`` is allowed to post at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from ..config import Settings
from ..pending import (
    ActionStatus,
    PendingAction,
    PendingActionNotFound,
    PendingActionStore,
)
from .audit import SYSTEM_ACTOR, AuditAction, AuditEvent, AuditLog
from .policy import ReviewDecision, ReviewPolicy

AUDIT_DB_SUFFIX = "-audit"


class ReviewOutcome(StrEnum):
    """What a review call did."""

    APPROVED = "approved"
    REJECTED = "rejected"
    ALREADY_APPROVED = "already_approved"
    ALREADY_REJECTED = "already_rejected"
    NOT_HELD = "not_held"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    REVIEW_DISABLED = "review_disabled"


#: Outcomes that mean the caller asked for something that cannot be done.
REFUSED_OUTCOMES = frozenset(
    {
        ReviewOutcome.NOT_HELD,
        ReviewOutcome.NOT_FOUND,
        ReviewOutcome.EXPIRED,
        ReviewOutcome.REVIEW_DISABLED,
    }
)


@dataclass(frozen=True)
class ReviewResult:
    outcome: ReviewOutcome
    action: PendingAction | None = None
    #: True only when this call performed the transition, so a second approval
    #: of the same action reports False and writes no second audit entry.
    changed: bool = False
    message: str = ""

    @property
    def is_refusal(self) -> bool:
        return self.outcome in REFUSED_OUTCOMES


def audit_path_for(settings: Settings) -> str:
    return f"{settings.state_db_path}{AUDIT_DB_SUFFIX}"


class ReviewService:
    """Policy, status transitions and the audit log, in one place."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: PendingActionStore | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self._settings = settings
        self._policy = ReviewPolicy.from_settings(settings)
        self._store = store or PendingActionStore(
            settings.state_db_path, ttl_seconds=settings.pending_ttl_seconds
        )
        self._audit = audit or AuditLog(audit_path_for(settings))

    @property
    def policy(self) -> ReviewPolicy:
        return self._policy

    @property
    def store(self) -> PendingActionStore:
        return self._store

    @property
    def audit(self) -> AuditLog:
        return self._audit

    @property
    def enabled(self) -> bool:
        return self._policy.enabled

    # ---------------------------------------------------------------- preparing

    def decide(self, *, loan_id: str, amount_cents: int) -> ReviewDecision:
        """Apply the policy to a payment about to be prepared."""
        if not self._policy.enabled:
            return ReviewDecision(needs_review=False)
        earlier = self._store.count_prepares_since(
            loan_id, since=_window_start(self._policy.window_seconds)
        )
        return self._policy.decide(amount_cents=amount_cents, earlier_prepares_in_window=earlier)

    def record_prepared(self, action: PendingAction, decision: ReviewDecision) -> None:
        """Write the audit entries for a freshly prepared action."""
        self._audit.record(
            actor=SYSTEM_ACTOR,
            action=AuditAction.PREPARED,
            pending_action_id=action.pending_action_id,
            loan_id=action.loan_id,
            amount_cents=action.amount_cents,
            old_status=None,
            new_status=action.status.value,
            reason=decision.reason_text() or None,
        )
        if action.status is ActionStatus.NEEDS_REVIEW:
            self._audit.record(
                actor=SYSTEM_ACTOR,
                action=AuditAction.HELD_FOR_REVIEW,
                pending_action_id=action.pending_action_id,
                loan_id=action.loan_id,
                amount_cents=action.amount_cents,
                old_status=ActionStatus.NEEDS_REVIEW.value,
                new_status=ActionStatus.NEEDS_REVIEW.value,
                reason=decision.reason_text() or None,
            )

    # ---------------------------------------------------------------- reviewing

    def held(self, *, limit: int = 100) -> list[PendingAction]:
        return self._store.list_by_status(ActionStatus.NEEDS_REVIEW, limit=limit)

    def approve(
        self, pending_action_id: str, *, actor: str, reason: str | None = None
    ) -> ReviewResult:
        return self._decide_held(
            pending_action_id,
            actor=actor,
            reason=reason,
            target=ActionStatus.APPROVED,
            audit_action=AuditAction.APPROVED,
            already=ReviewOutcome.ALREADY_APPROVED,
            done=ReviewOutcome.APPROVED,
        )

    def reject(self, pending_action_id: str, *, actor: str, reason: str) -> ReviewResult:
        return self._decide_held(
            pending_action_id,
            actor=actor,
            reason=reason,
            target=ActionStatus.REJECTED,
            audit_action=AuditAction.REJECTED,
            already=ReviewOutcome.ALREADY_REJECTED,
            done=ReviewOutcome.REJECTED,
        )

    def _decide_held(
        self,
        pending_action_id: str,
        *,
        actor: str,
        reason: str | None,
        target: ActionStatus,
        audit_action: AuditAction,
        already: ReviewOutcome,
        done: ReviewOutcome,
    ) -> ReviewResult:
        if not self._policy.enabled:
            return ReviewResult(
                outcome=ReviewOutcome.REVIEW_DISABLED,
                message="The review queue is not enabled, so there is nothing to review.",
            )

        try:
            action = self._store.get(pending_action_id)
        except PendingActionNotFound:
            return ReviewResult(
                outcome=ReviewOutcome.NOT_FOUND,
                message="There is no prepared payment with that id.",
            )

        if action.status is target:
            return ReviewResult(
                outcome=already,
                action=action,
                changed=False,
                message=f"That payment was already {target.value}.",
            )
        if action.status is not ActionStatus.NEEDS_REVIEW:
            return ReviewResult(
                outcome=ReviewOutcome.NOT_HELD,
                action=action,
                message=(
                    f"That payment is {action.status.value}, not waiting for review, "
                    "so there is nothing to decide."
                ),
            )
        if action.is_expired():
            # Recorded, because a reviewer trying to act on an expired action is
            # worth seeing in the trail.
            self._audit.record(
                actor=actor,
                action=audit_action,
                pending_action_id=action.pending_action_id,
                loan_id=action.loan_id,
                amount_cents=action.amount_cents,
                old_status=action.status.value,
                new_status=action.status.value,
                reason="refused: the prepared payment had expired",
            )
            return ReviewResult(
                outcome=ReviewOutcome.EXPIRED,
                action=action,
                message="That prepared payment has expired, so it cannot be approved.",
            )

        # The conditional update is what makes two racing reviewers produce one
        # transition. Whoever loses sees changed=False and writes no entry.
        changed = self._store.transition(
            pending_action_id, expected=ActionStatus.NEEDS_REVIEW, new=target
        )
        updated = self._store.get(pending_action_id)
        if not changed:
            return ReviewResult(
                outcome=already if updated.status is target else ReviewOutcome.NOT_HELD,
                action=updated,
                changed=False,
                message=f"That payment was already {updated.status.value}.",
            )

        self._audit.record(
            actor=actor,
            action=audit_action,
            pending_action_id=action.pending_action_id,
            loan_id=action.loan_id,
            amount_cents=action.amount_cents,
            old_status=ActionStatus.NEEDS_REVIEW.value,
            new_status=target.value,
            reason=reason,
        )
        return ReviewResult(
            outcome=done, action=updated, changed=True, message=f"Payment {target.value}."
        )

    # ---------------------------------------------------------------- confirming

    def record_confirm_refused(self, action: PendingAction, *, reason: str) -> None:
        self._audit.record(
            actor=SYSTEM_ACTOR,
            action=AuditAction.CONFIRM_REFUSED,
            pending_action_id=action.pending_action_id,
            loan_id=action.loan_id,
            amount_cents=action.amount_cents,
            old_status=action.status.value,
            new_status=action.status.value,
            reason=reason,
        )

    def record_confirmed(
        self, action: PendingAction, *, first_time: bool, payment_id: str | None
    ) -> None:
        """Record a posted confirmation, distinguishing the first from a replay."""
        self._audit.record(
            actor=SYSTEM_ACTOR,
            action=AuditAction.CONFIRMED if first_time else AuditAction.CONFIRMED_REPLAY,
            pending_action_id=action.pending_action_id,
            loan_id=action.loan_id,
            amount_cents=action.amount_cents,
            old_status=action.status.value,
            new_status=action.status.value,
            reason=None if payment_id is None else f"payment {payment_id}",
        )

    def entries_for(self, pending_action_id: str, *, limit: int = 200) -> list[AuditEvent]:
        return self._audit.entries(pending_action_id=pending_action_id, limit=limit)


def _window_start(window_seconds: int) -> float:
    return time.time() - max(0, window_seconds)
