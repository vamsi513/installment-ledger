"""Redaction for log lines.

Loan ids are account identifiers. They go into logs as the last four characters
only, which is enough to follow one conversation through a log stream and not
enough to address the loan.
"""

from __future__ import annotations

VISIBLE_SUFFIX_LENGTH = 4
REDACTED_PREFIX = "..."


def redact_loan_id(loan_id: str | None) -> str:
    """Return a loan id as its last four characters behind an ellipsis."""
    if not loan_id:
        return "(none)"
    text = str(loan_id)
    if len(text) <= VISIBLE_SUFFIX_LENGTH:
        return REDACTED_PREFIX + text
    return REDACTED_PREFIX + text[-VISIBLE_SUFFIX_LENGTH:]


def redact_session_id(session_id: str | None) -> str:
    """Session ids are redacted the same way; they are caller supplied strings."""
    return redact_loan_id(session_id)
