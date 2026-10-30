"""Every word the user reads.

Replies are templates filled from tool output. The model does not compose them,
which is what makes "never claim success that did not happen" something the code
guarantees rather than something the prompt asks for. A number in a reply came
from the ledger or it is not in the reply.
"""

from __future__ import annotations

from typing import Any

OUT_OF_SCOPE_REPLIES: dict[str, str] = {
    "other_customers": (
        "I can only look at the loans in this session. I cannot see other people's "
        "accounts or loans."
    ),
    "financial_advice": (
        "I cannot give financial advice. I can tell you what you owe, show your "
        "schedule and take a payment you ask for."
    ),
    "legal_or_tax": (
        "I cannot help with tax or legal questions. I can tell you what you owe, show "
        "your schedule and take a payment you ask for."
    ),
    "unsupported_action": (
        "I cannot change a loan. There are no late fees to waive, and I cannot alter a "
        "due date, reverse a payment or close an account. I can tell you what you owe, "
        "show your schedule and take a payment you ask for."
    ),
    "confirmation_bypass": (
        "I will not skip the confirmation step. Every payment is previewed first and "
        "only goes through after you reply yes to it. Tell me an amount and I will show "
        "you what it would do."
    ),
    "generic": (
        "I cannot help with that. I can tell you what you owe, show your schedule and "
        "take a payment you ask for."
    ),
}

REFUSAL_REPLIES: dict[str, str] = {
    "empty_message": "I did not get a message. Ask me about your balance or a payment.",
    "amount_missing": (
        "Tell me how much you want to pay and I will check it against your balance first."
    ),
    "invalid_amount": "A payment has to be a positive amount, so I cannot use that one.",
    "no_loan_in_session": "This session has no loan attached, so there is nothing for me to read.",
    "ambiguous_loan": (
        "This session has more than one loan. Tell me which loan id you mean and I will "
        "use that one."
    ),
    "loan_not_in_session": (
        "That loan is not one of the loans in this session, so I will not touch it."
    ),
    "nothing_to_confirm": (
        "There is nothing waiting to be confirmed. Tell me an amount and I will prepare "
        "a payment for you to confirm."
    ),
}


def amount_above_cap_reply(amount_cents: int, cap_cents: int) -> str:
    return (
        f"I cannot take a single payment of {amount_cents} cents. Payments through this "
        f"assistant are capped at {cap_cents} cents, so this one needs to go through "
        f"another channel."
    )


def cancelled_reply() -> str:
    return "Cancelled. Nothing was paid."


def loan_summary_reply(payload: dict[str, Any]) -> str:
    outstanding = payload["outstanding_cents"]
    total = payload["total_cents"]
    paid = payload["paid_cents"]
    status = payload["loan_status"]
    unpaid = [item for item in payload.get("installments", []) if item["outstanding_cents"] > 0]

    lines = [
        f"This loan is {status}. You owe {outstanding} cents of {total} cents in total, "
        f"and {paid} cents has been paid."
    ]
    if unpaid:
        head = unpaid[0]
        lines.append(
            f"The next installment is number {head['sequence']}, due {head['due_date']}, "
            f"with {head['outstanding_cents']} cents still on it."
        )
        lines.append(f"There are {len(unpaid)} installments with something left to pay.")
    else:
        lines.append("Every installment is paid off.")
    return " ".join(lines)


def ledger_reply(payload: dict[str, Any]) -> str:
    lines = payload.get("lines", [])
    if not lines:
        return "There are no ledger entries on this loan yet."
    rendered = "; ".join(
        f"{line['direction']} {line['amount_cents']} cents to {line['account']}"
        for line in lines[:6]
    )
    tail = (
        f" Showing {min(len(lines), 6)} of {payload.get('returned', len(lines))} returned lines."
        if len(lines) > 6
        else ""
    )
    return f"The most recent ledger lines are: {rendered}.{tail}"


def payment_preview_reply(payload: dict[str, Any]) -> str:
    covers = payload.get("covers", [])
    if covers:
        covered = ", ".join(
            f"installment {item['sequence']} ({item['applied_cents']} cents"
            f"{', paid off' if item['clears_installment'] else ''})"
            for item in covers
        )
        where = f" It would go to {covered}."
    else:
        where = ""
    return (
        f"I can pay {payload['amount_cents']} cents, which would leave "
        f"{payload['outstanding_after_cents']} cents outstanding.{where} "
        "Reply yes to confirm, or no to cancel. Nothing has been paid yet."
    )


def payment_applied_reply(payload: dict[str, Any]) -> str:
    closed = payload.get("loan_status") == "closed"
    tail = " The loan is now paid off." if closed else ""
    return (
        f"Paid {payload['amount_cents']} cents. The outstanding balance is now "
        f"{payload['outstanding_after_cents']} cents.{tail}"
    )


def already_applied_reply(payload: dict[str, Any]) -> str:
    return (
        f"That payment of {payload['amount_cents']} cents was already applied, so I did "
        f"not take it twice. The outstanding balance is "
        f"{payload['outstanding_after_cents']} cents."
    )


LEDGER_REJECTION_REPLIES: dict[str, str] = {
    "overpayment": (
        "That is more than the balance on this loan, so the ledger refused it and nothing was paid."
    ),
    "loan_closed": "This loan is already paid off, so nothing was paid.",
    "loan_not_found": "I could not find that loan, so there is nothing to report on it.",
    "pending_action_expired": (
        "That payment sat unconfirmed for too long and has expired. Tell me the amount "
        "again and I will prepare a fresh one."
    ),
    "pending_action_not_found": (
        "I no longer have that prepared payment. Tell me the amount again and I will "
        "prepare a fresh one."
    ),
    "idempotency_key_conflict": (
        "The ledger has already seen this confirmation with different details, so it "
        "refused it and nothing new was paid. Ask me to check the balance."
    ),
    "amount_above_cap": ("That amount is above the per payment cap, so nothing was paid."),
    "invalid_amount": "A payment has to be a positive amount, so nothing was paid.",
}


def ledger_rejection_reply(code: str | None, message: str | None) -> str:
    if code and code in LEDGER_REJECTION_REPLIES:
        return LEDGER_REJECTION_REPLIES[code]
    detail = f" The ledger said: {message}" if message else ""
    return f"The ledger refused that and nothing was changed.{detail}"


def unknown_outcome_reply() -> str:
    """Said when a write timed out. It must not claim either result."""
    return (
        "I did not get an answer back in time, so I do not know whether that payment "
        "was applied. I am not going to send it again, because that could pay twice. "
        "Ask me to check your balance and I will tell you what the ledger actually has."
    )


def unavailable_reply(*, was_writing: bool) -> str:
    if was_writing:
        return "I could not reach the ledger, so nothing was paid. Try again in a moment."
    return "I could not reach the ledger, so I cannot tell you anything about this loan right now."


def tool_failure_reply() -> str:
    return (
        "Something went wrong on my side and I could not complete that. Nothing was "
        "changed. Please try again."
    )
