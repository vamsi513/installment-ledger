"""Deterministic checks.

Everything here is a rule over the user's text or over an integer. None of it
asks a model anything. These are the checks the safety claims rest on:

* a confirmation is an exact match against a short allowlist of phrases, so the
  model never gets to decide that the user agreed;
* an out of scope message is recognised by pattern and refused with fixed text
  before any model call is made;
* an amount is checked against the cap, against zero and against the loan being
  one this session is allowed to touch, before any tool runs.
"""

from __future__ import annotations

import re
import unicodedata

# An exact, full string match is required. "yes" confirms. "yes but make it 50"
# does not, and neither does "yes" with a number anywhere in the message.
CONFIRMATION_PHRASES = frozenset(
    {
        "y",
        "yes",
        "yes please",
        "yes do it",
        "yes confirm",
        "yes confirm it",
        "yes go ahead",
        "yes thats right",
        "confirm",
        "confirm it",
        "confirm please",
        "confirmed",
        "please confirm",
        "go ahead",
        "go ahead please",
        "do it",
        "do it please",
        "approve",
        "approved",
        "ok confirm",
        "okay confirm",
        "that is correct",
        "thats correct",
        "correct",
        "proceed",
        "yes proceed",
    }
)

CANCELLATION_PHRASES = frozenset(
    {
        "n",
        "no",
        "no thanks",
        "no thank you",
        "cancel",
        "cancel it",
        "cancel that",
        "stop",
        "stop it",
        "nevermind",
        "never mind",
        "forget it",
        "dont",
        "do not",
        "abort",
        "no dont",
        "no do not",
    }
)

_UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)

# Reason code to pattern. Checked in order; the first match wins.
OUT_OF_SCOPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        # Checked first. A message that asks for the confirmation step to be
        # skipped is refused outright rather than quietly having the step
        # applied anyway, so the user is told the control exists.
        "confirmation_bypass",
        re.compile(
            r"\bwithout (asking|confirmation|confirming|my ok|my approval)\b"
            r"|\bno need to (ask|confirm)\b|\bno confirmation\b"
            r"|\bskip (the |any )?confirm\w*|\bbypass (the )?confirm\w*"
            r"|\bdon'?t (ask|confirm)\b|\bdo not (ask|confirm)\b"
            r"|\bwithout (a |any )?(preview|check)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "other_customers",
        re.compile(
            r"\b(other|another|someone else'?s?|somebody else'?s?|everyone'?s?|all)\s+"
            r"(customers?|borrowers?|users?|accounts?|clients?|people'?s?)\b"
            r"|\b(customers?|borrowers?|accounts?)\s+(list|database|table)\b"
            r"|\ball\s+loans\b|\bevery\s+loan\b|\bother\s+loans?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "financial_advice",
        re.compile(
            r"\bshould i\b|\bwould you (recommend|advise|suggest)\b|\badvice\b|\badvise\b"
            r"|\bis it (worth|better|smarter|wise)\b|\bbetter off\b|\bwhat would you do\b"
            r"|\brefinanc\w*|\binvest\w*|\bcredit score\b|\bcredit rating\b"
            r"|\bshould we\b|\bbest option\b",
            re.IGNORECASE,
        ),
    ),
    (
        "legal_or_tax",
        re.compile(
            r"\btax(es|able|deduct\w*)?\b|\blawyer\b|\battorney\b|\blegal (advice|opinion)\b"
            r"|\bbankrupt\w*|\bsue\b|\blawsuit\b",
            re.IGNORECASE,
        ),
    ),
    (
        "unsupported_action",
        re.compile(
            r"\bcancel (my|the|this) loan\b|\bclose (my|the) account\b|\bdelete (my|the)\b"
            r"|\bchange (my|the) (due date|schedule|instal?ment)\b|\breschedul\w*"
            r"|\bwaive\b|\blate fee\b|\bpenalt\w*|\brefund\b|\breverse\b|\bvoid\b"
            r"|\bdispute\b|\bchargeback\b|\bnew loan\b|\bapply for\b|\bincrease (my|the) limit\b"
            r"|\bskip (a|my|the|this) payment\b|\bpause (my|the) (loan|payments?)\b"
            r"|\bchange (my|the) (apr|interest|rate)\b",
            re.IGNORECASE,
        ),
    ),
)

# Recognised only so it can be logged and counted. Detection does not change the
# answer: an injected instruction is still just text, and a write still needs a
# prepared action plus an exact confirmation phrase.
INJECTION_MARKERS = re.compile(
    r"\bignore (all |any |your |the )?(previous|prior|earlier|above)\b"
    r"|\bdisregard (all |any |your |the )?(previous|prior|earlier|above)\b"
    r"|\byou are now\b|\bnew instructions?\b|\bsystem prompt\b|\bdeveloper mode\b"
    r"|\boverride\b|\bas an admin\b|\bpretend (you|to be)\b|\bwithout (asking|confirmation)\b"
    r"|\bno need to confirm\b|\bskip (the )?confirmation\b",
    re.IGNORECASE,
)

# A negative amount is detected from the text, not from the number the model
# pulled out, because an extractor that drops a minus sign would otherwise turn
# "pay -5 dollars" into a positive payment.
NEGATIVE_AMOUNT = re.compile(
    r"(?:^|[\s(\[])[-\u2212\u2013]\s*\d|\b(?:minus|negative)\s+[\d$]", re.IGNORECASE
)

_CONTROL_CHARACTERS = {"Cc", "Cf", "Cs", "Co", "Cn"}


def sanitize_message(raw: str, *, max_chars: int) -> str:
    """Normalise user text before anything looks at it.

    Unicode is folded to NFKC, control and format characters are dropped so a
    zero width joiner cannot hide a word from the pattern checks, whitespace is
    collapsed, and the result is truncated.
    """
    folded = unicodedata.normalize("NFKC", raw or "")
    kept = [
        character
        for character in folded
        if character in "\t\n\r" or unicodedata.category(character) not in _CONTROL_CHARACTERS
    ]
    collapsed = re.sub(r"\s+", " ", "".join(kept)).strip()
    return collapsed[:max_chars]


def normalize_phrase(text: str) -> str:
    """Reduce text to lowercase words for an exact phrase comparison."""
    lowered = unicodedata.normalize("NFKC", text or "").lower()
    stripped = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", stripped).strip()


def contains_digit(text: str) -> bool:
    return any(character.isdigit() for character in text or "")


def is_explicit_confirmation(text: str) -> bool:
    """True only for an exact confirmation phrase with no digits in it.

    The digit rule matters: "yes, 50000" is a new amount, not agreement to the
    amount already on the table.
    """
    if contains_digit(text):
        return False
    return normalize_phrase(text) in CONFIRMATION_PHRASES


def is_explicit_cancellation(text: str) -> bool:
    return normalize_phrase(text) in CANCELLATION_PHRASES


def out_of_scope_reason(text: str) -> str | None:
    """Return a reason code when the message is something the tools cannot do."""
    for reason, pattern in OUT_OF_SCOPE_PATTERNS:
        if pattern.search(text or ""):
            return reason
    return None


def looks_like_injection(text: str) -> bool:
    return bool(INJECTION_MARKERS.search(text or ""))


def mentions_negative_amount(text: str) -> bool:
    """True when the message writes an amount as a negative number."""
    return bool(NEGATIVE_AMOUNT.search(text or ""))


def find_loan_reference(text: str) -> str | None:
    """Return the first loan UUID written out in the message, if any."""
    match = _UUID_PATTERN.search(text or "")
    return match.group(0).lower() if match else None


def resolve_target_loan(text: str, bound_loan_ids: list[str]) -> tuple[str | None, str | None]:
    """Decide which loan a message is about.

    Returns the loan id and a refusal code. A loan written out in the message
    must be one this session is bound to. With no loan in the message, a session
    holding exactly one loan uses it, and a session holding several has to be
    told which.
    """
    bound = [str(loan_id).lower() for loan_id in bound_loan_ids or []]
    mentioned = find_loan_reference(text)
    if mentioned is not None:
        if mentioned not in bound:
            return None, "loan_not_in_session"
        return mentioned, None
    if not bound:
        return None, "no_loan_in_session"
    if len(bound) == 1:
        return bound[0], None
    return None, "ambiguous_loan"


def check_amount(amount_cents: int | None, *, max_payment_cents: int) -> str | None:
    """Return a refusal code for an unacceptable amount, or None."""
    if amount_cents is None:
        return "amount_missing"
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool):
        return "invalid_amount"
    if amount_cents <= 0:
        return "invalid_amount"
    if amount_cents > max_payment_cents:
        return "amount_above_cap"
    return None
