"""A human review step for risky prepared payments.

Off unless ``ASSISTANT_REVIEW_ENABLED`` says otherwise. With it off, nothing in
this package runs and the assistant behaves exactly as it did before: every
prepared payment is ``ready`` and ``confirm_payment`` posts it on an explicit
confirmation.

With it on, a prepared payment the policy considers risky is held as
``needs_review``, ``confirm_payment`` refuses it until a reviewer approves, and
every status change is written to an append only audit log.
"""

from .audit import AuditEvent, AuditLog
from .policy import ReviewDecision, ReviewPolicy
from .service import ReviewOutcome, ReviewResult, ReviewService

__all__ = [
    "AuditEvent",
    "AuditLog",
    "ReviewDecision",
    "ReviewOutcome",
    "ReviewPolicy",
    "ReviewResult",
    "ReviewService",
]
