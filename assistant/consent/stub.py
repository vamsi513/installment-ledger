"""Deterministic stand ins for the consent model.

:class:`StubConsentModel` is rule based, like ``assistant.llm.fake``. It returns
the real schema through the real code path, so what the offline tests and the
offline eval exercise is the gate, the threshold, the fallback and the logging.
It is NOT a model and a score it produces says nothing about one.

It is deliberately a bit worse than a model at the thing a model is better at,
free phrasing, and better than one at the thing a keyword rule is better at,
never wavering. Where it reports a confidence, that number is a fixed constant
chosen to sit either side of a plausible threshold so the threshold logic can be
exercised; it is not a calibrated probability and must not be read as one.

:class:`ScriptedConsentModel` returns whatever a test queues up, including
raising, which is how the timeout, transport error and invalid output paths are
driven without a network.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Iterable

from .model import ConsentModelError
from .schema import ConsentClassification, ConsentLabel

#: Fixed confidences. High sits above any threshold in the swept range and low
#: sits below all of it, so a test naming one of these is naming a side of the
#: threshold rather than a number that happens to work.
HIGH_CONFIDENCE = 0.97
MEDIUM_CONFIDENCE = 0.6
LOW_CONFIDENCE = 0.2

_REPLY_BLOCK = re.compile(r"The user replied:\s*(?P<reply>.*)\s*\Z", re.DOTALL)

_DECLINE = re.compile(
    r"\b(no|nope|nah|don'?t|do not|cancel|stop|abort|forget it|never ?mind"
    r"|not now|not yet|hold off|leave it|skip it|changed my mind)\b",
    re.IGNORECASE,
)

_CONFIRM = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|approve[d]?|go ahead|do it"
    r"|please do|sounds good|that'?s right|correct|proceed|send it|pay it)\b",
    re.IGNORECASE,
)

_QUESTION = re.compile(
    r"\?|^\s*(what|why|how|when|which|who|can you|could you|will that|does that|is that)\b",
    re.IGNORECASE,
)

# "make it 25 dollars", "change it to 2500 cents", "100 instead"
_AMOUNT = re.compile(
    r"(?P<whole>\d[\d,_]*)(?:\.(?P<frac>\d{1,2}))?\s*(?P<unit>cents?|dollars?|usd|bucks)?",
    re.IGNORECASE,
)
_MODIFY_HINT = re.compile(
    r"\b(make it|change it to|change to|instead|rather than|but only|only pay"
    r"|just pay|how about|let'?s do|lower it|raise it|put)\b",
    re.IGNORECASE,
)


def extract_reply(user_prompt: str) -> str:
    """Pull the user's reply back out of the rendered prompt."""
    match = _REPLY_BLOCK.search(user_prompt or "")
    return (match.group("reply") if match else (user_prompt or "")).strip()


def _amount_cents(text: str) -> int | None:
    """Read a modified amount as integer cents, or give up.

    A bare number is read as dollars, which is how people say it out loud. More
    than two decimal places is not money and is refused rather than rounded.
    """
    for match in _AMOUNT.finditer(text):
        whole = match.group("whole")
        if whole is None:
            continue
        unit = (match.group("unit") or "").lower()
        digits = whole.replace(",", "").replace("_", "")
        frac = match.group("frac")
        if unit.startswith("cent"):
            if frac is not None:
                return None
            return int(digits)
        cents = int(digits) * 100
        if frac is not None:
            cents += int(frac.ljust(2, "0"))
        return cents
    return None


class StubConsentModel:
    """Keyword rules returning the real schema. Records what it was asked."""

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def classify(self, *, system: str, user: str) -> ConsentClassification:
        reply = extract_reply(user)
        self.calls.append(reply)
        return self.decide(reply)

    def decide(self, reply: str) -> ConsentClassification:
        text = (reply or "").strip()
        if not text:
            return ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=LOW_CONFIDENCE)

        wants_modify = bool(_MODIFY_HINT.search(text))
        amount = _amount_cents(text) if wants_modify else None

        # A modify is checked before a confirm, because "yes, but make it 100"
        # contains a confirmation word and is not a confirmation.
        if wants_modify and amount is not None and amount > 0:
            return ConsentClassification(
                label=ConsentLabel.MODIFY,
                confidence=HIGH_CONFIDENCE,
                modified_amount_cents=amount,
            )
        if wants_modify:
            # Wanted something changed but did not say what to. Asking again is
            # the only safe reading.
            return ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=LOW_CONFIDENCE)

        if _DECLINE.search(text):
            return ConsentClassification(label=ConsentLabel.DECLINE, confidence=HIGH_CONFIDENCE)

        if _QUESTION.search(text):
            return ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=LOW_CONFIDENCE)

        if _CONFIRM.search(text):
            # A long reply that merely contains "yes" is weaker evidence than a
            # short one that is "yes", and the threshold is where that lands.
            words = len(text.split())
            confidence = HIGH_CONFIDENCE if words <= 4 else MEDIUM_CONFIDENCE
            return ConsentClassification(label=ConsentLabel.CONFIRM, confidence=confidence)

        return ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=LOW_CONFIDENCE)


class ScriptedConsentModel:
    """Returns queued answers, raises queued exceptions, or hangs.

    Used by the tests to drive the paths a rule based stub cannot reach: a
    transport error, an answer that fails validation, and a call that never
    returns inside the timeout.
    """

    name = "scripted"

    def __init__(
        self,
        answers: Iterable[ConsentClassification | BaseException] = (),
        *,
        hang_seconds: float | None = None,
    ) -> None:
        self._answers: deque[ConsentClassification | BaseException] = deque(answers)
        self._hang_seconds = hang_seconds
        self.calls: list[str] = []

    async def classify(self, *, system: str, user: str) -> ConsentClassification:
        self.calls.append(extract_reply(user))
        if self._hang_seconds is not None:
            await asyncio.sleep(self._hang_seconds)
        if not self._answers:
            raise ConsentModelError("the scripted consent model ran out of answers")
        answer = self._answers.popleft()
        if isinstance(answer, BaseException):
            raise answer
        return answer
