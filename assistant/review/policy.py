"""When a prepared payment needs a person to look at it.

Two rules, both configurable, both off when their setting is zero:

* the amount is above a threshold;
* the same loan has had more than N payments prepared inside a window.

The thresholds that ship as defaults are examples chosen to be easy to reason
about. They are not advice about where a real limit belongs, and a real service
would want rules this module does not have, such as anything about the borrower
or the device the request came from.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings

AMOUNT_ABOVE_THRESHOLD = "amount_above_threshold"
TOO_MANY_PREPARES = "too_many_prepares_in_window"


@dataclass(frozen=True)
class ReviewDecision:
    """Whether to hold an action, and every rule that said so."""

    needs_review: bool
    reasons: tuple[str, ...] = ()

    def reason_text(self) -> str:
        return ", ".join(self.reasons)


@dataclass(frozen=True)
class ReviewPolicy:
    enabled: bool
    threshold_cents: int
    max_prepares: int
    window_seconds: int

    @classmethod
    def from_settings(cls, settings: Settings) -> ReviewPolicy:
        return cls(
            enabled=settings.review_enabled,
            threshold_cents=settings.review_threshold_cents,
            max_prepares=settings.review_max_prepares,
            window_seconds=settings.review_window_seconds,
        )

    def decide(self, *, amount_cents: int, earlier_prepares_in_window: int) -> ReviewDecision:
        """Decide on one prepared payment.

        ``earlier_prepares_in_window`` counts the payments already prepared for
        this loan inside the window, not counting the one being decided. So
        "more than N in the window" is reached when that count is already N.
        """
        if not self.enabled:
            return ReviewDecision(needs_review=False)

        reasons: list[str] = []
        if self.threshold_cents > 0 and amount_cents > self.threshold_cents:
            reasons.append(AMOUNT_ABOVE_THRESHOLD)
        if self.max_prepares > 0 and earlier_prepares_in_window >= self.max_prepares:
            reasons.append(TOO_MANY_PREPARES)

        return ReviewDecision(needs_review=bool(reasons), reasons=tuple(reasons))
