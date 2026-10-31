"""A deterministic stand in for the model, used by the offline tests and the
eval harness dry run.

It is not a mock of an HTTP call. It is a small rule based classifier that
returns exactly the JSON shape the real prompt asks for, so the agent code under
test is the same code that runs in production and only the model differs.
"""

from __future__ import annotations

import json
import re

from .base import LlmReply, Message

_SIGN = r"(?P<sign>-|\u2212|minus\s+|negative\s+)?"
_AMOUNT_PATTERNS = (
    # "12345 cents", "-500 cents"
    re.compile(_SIGN + r"(?P<cents>\d[\d,_]*)\s*(?:cents|cent|c)\b", re.IGNORECASE),
    # "$123.45", "-$5"
    re.compile(_SIGN + r"\$\s*(?P<whole>\d[\d,_]*)(?:\.(?P<frac>\d{1,2}))?"),
    re.compile(
        _SIGN + r"(?P<whole>\d[\d,_]*)(?:\.(?P<frac>\d{1,2}))?\s*(?:dollars|usd|bucks)\b",
        re.IGNORECASE,
    ),
)

_PAYMENT_WORDS = ("pay", "payment", "settle", "put down", "send", "transfer", "charge")
_READ_WORDS = (
    "balance",
    "owe",
    "owing",
    "outstanding",
    "status",
    "schedule",
    "installment",
    "instalment",
    "ledger",
    "entries",
    "history",
    "due",
    "left",
    "remaining",
    "summary",
)


def _digits(text: str) -> str:
    return text.replace(",", "").replace("_", "")


def extract_amount_cents(text: str) -> int | None:
    """Pull an integer cent amount out of free text, or return None.

    Dollar amounts become cents. Anything with more than two decimal places is
    not treated as money at all, because guessing would be worse than refusing.
    """
    for pattern in _AMOUNT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groupdict()
        sign = -1 if groups.get("sign") else 1
        if groups.get("cents") is not None:
            return sign * int(_digits(groups["cents"]))
        whole = int(_digits(groups["whole"]))
        frac = groups.get("frac") or "0"
        return sign * (whole * 100 + int(frac.ljust(2, "0")))
    return None


class FakeLlmClient:
    """Classifies with keyword rules and reports zero tokens."""

    name = "fake"

    def __init__(self, *, model: str = "fake-classifier") -> None:
        self.model = model
        self.calls: list[list[Message]] = []

    async def complete(self, messages: list[Message], *, max_output_tokens: int = 256) -> LlmReply:
        self.calls.append(messages)
        user_text = next(
            (message.content for message in reversed(messages) if message.role == "user"), ""
        )
        payload = self._classify(user_text)
        return LlmReply(
            text=json.dumps(payload),
            prompt_tokens=0,
            completion_tokens=0,
            model=self.model,
        )

    def _classify(self, text: str) -> dict[str, object]:
        lowered = text.lower()
        amount = extract_amount_cents(text)

        wants_payment = any(word in lowered for word in _PAYMENT_WORDS)
        wants_read = any(word in lowered for word in _READ_WORDS)

        if wants_payment and amount is not None:
            return {"intent": "payment_request", "amount_cents": amount, "wants_ledger": False}
        if wants_payment and amount is None:
            # A payment with no amount is not actionable, so it is answered as a
            # question rather than guessed at.
            return {"intent": "read_question", "amount_cents": None, "wants_ledger": False}
        if wants_read:
            wants_ledger = any(word in lowered for word in ("ledger", "entries", "history"))
            return {"intent": "read_question", "amount_cents": None, "wants_ledger": wants_ledger}
        return {"intent": "out_of_scope", "amount_cents": None, "wants_ledger": False}
