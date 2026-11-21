"""The gate: one place that turns a reply into a decision about a proposal.

The threshold, the timeout, the fallback and the mode all live here, so the
graph node that calls it stays a few lines and so the behaviour can be tested
without a graph, a ledger or a network.

What the modes mean:

``rule``    the gate is not called at all. The graph keeps the code path it has
            today, which is why the default changes nothing.
``shadow``  the classifier runs and is recorded, and the existing rule decides.
            The outcome is the rule's outcome. Nothing the classifier says can
            change what happens.
``live``    the classifier decides, through the threshold. The existing rule is
            the fallback when the classifier cannot be used.

Two things the classifier can never do, in any mode:

* skip the review queue. Review is enforced in the MCP server, in
  ``prepare_payment`` and ``confirm_payment``, which are downstream of every
  decision made here. A ``modify`` becomes a fresh proposal and goes through
  ``prepare_payment`` again, so it is policy checked like any other.
* act on a modified amount. ``repropose`` carries the amount back to the
  proposal step; it is never handed to ``confirm_payment``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import StrEnum

from .log import ConsentAction, ConsentFailure, ConsentLog, DecidedBy
from .model import SYSTEM_PROMPT, ConsentModel, ConsentModelError, build_user_prompt
from .schema import ConsentClassification, ConsentLabel

logger = logging.getLogger("assistant.consent")


class ConsentMode(StrEnum):
    RULE = "rule"
    SHADOW = "shadow"
    LIVE = "live"


@dataclass(frozen=True)
class Proposal:
    """The payment the user was asked about."""

    amount_cents: int
    payee: str = ""
    installment: str = ""
    pending_action_id: str | None = None
    loan_id: str | None = None


@dataclass(frozen=True)
class ConsentOutcome:
    """What the gate decided, and how it got there."""

    action: ConsentAction
    decided_by: DecidedBy
    rule_confirm: bool
    classification: ConsentClassification | None = None
    failure: ConsentFailure | None = None
    agreed: bool | None = None
    modified_amount_cents: int | None = None
    latency_ms: int = 0

    @property
    def used_classifier(self) -> bool:
        return self.decided_by is DecidedBy.CLASSIFIER


def _rule_action(rule_confirm: bool) -> ConsentAction:
    """What the existing rule means, expressed as a gate action.

    An exact confirmation phrase acts. Anything else is not agreement, and the
    agent asking again is what the graph already does with it today.
    """
    return ConsentAction.ACT if rule_confirm else ConsentAction.ASK_AGAIN


def _classifier_action(classification: ConsentClassification, *, threshold: float) -> ConsentAction:
    """Apply the threshold. The only place a label becomes an action."""
    label = classification.label
    if label is ConsentLabel.DECLINE:
        return ConsentAction.CANCEL
    if label is ConsentLabel.MODIFY:
        return ConsentAction.REPROPOSE
    if label is ConsentLabel.CONFIRM and classification.confidence >= threshold:
        return ConsentAction.ACT
    # Unclear, or a confirm the model is not sure enough about.
    return ConsentAction.ASK_AGAIN


async def classify_reply(
    model: ConsentModel,
    *,
    reply: str,
    proposal: Proposal,
    timeout_seconds: float,
) -> tuple[ConsentClassification | None, ConsentFailure | None]:
    """One classifier call, with the timeout enforced here.

    The timeout is applied with ``wait_for`` rather than left to the client, so
    it holds for any implementation and so the deadline is the one in
    configuration rather than whatever an SDK defaults to.
    """
    user = build_user_prompt(
        amount_cents=proposal.amount_cents,
        payee=proposal.payee or (proposal.loan_id or ""),
        installment=proposal.installment,
        reply=reply,
    )
    try:
        classification = await asyncio.wait_for(
            model.classify(system=SYSTEM_PROMPT, user=user), timeout=timeout_seconds
        )
    except TimeoutError:
        return None, ConsentFailure.TIMEOUT
    except ConsentModelError:
        return None, ConsentFailure.MODEL_ERROR
    except Exception:
        # An implementation that raises something unexpected is still an
        # implementation that did not answer. Falling back is the safe reading,
        # and the type is logged so it is not invisible.
        logger.warning("consent classifier raised an unexpected error", exc_info=True)
        return None, ConsentFailure.MODEL_ERROR
    return classification, None


async def decide_consent(
    *,
    mode: ConsentMode,
    reply: str,
    proposal: Proposal,
    model: ConsentModel,
    rule_confirm: bool,
    threshold: float,
    timeout_seconds: float,
    log: ConsentLog | None = None,
    session_id: str | None = None,
) -> ConsentOutcome:
    """Decide what to do with one reply to one proposal, and record it.

    Called only when a proposal is pending and the mode is shadow or live. In
    rule mode the graph does not call this at all.

    ``rule_confirm`` is what the existing confirmation rule said about this
    reply. It is passed in rather than computed here for two reasons: this
    package must not import the agent package, which builds it, and the rule
    lives in exactly one place on purpose. A second caller computing it a second
    way is how two rules that are supposed to be one drift apart.
    """
    started = time.monotonic()
    classification, failure = await classify_reply(
        model, reply=reply, proposal=proposal, timeout_seconds=timeout_seconds
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    if classification is None:
        # Timeout, transport failure or an answer that did not validate. The
        # existing rule decides, exactly as it does today, in both modes.
        outcome = ConsentOutcome(
            action=_rule_action(rule_confirm),
            decided_by=DecidedBy.RULE_FALLBACK,
            rule_confirm=rule_confirm,
            failure=failure,
            latency_ms=latency_ms,
        )
    else:
        classifier_action = _classifier_action(classification, threshold=threshold)
        # Agreement compares the two decisions on the question the rule can
        # answer: act, or do not act. The rule has no opinion about the
        # difference between declining, reproposing and asking again.
        agreed = (classifier_action is ConsentAction.ACT) == rule_confirm
        if mode is ConsentMode.LIVE:
            outcome = ConsentOutcome(
                action=classifier_action,
                decided_by=DecidedBy.CLASSIFIER,
                rule_confirm=rule_confirm,
                classification=classification,
                agreed=agreed,
                modified_amount_cents=(
                    classification.modified_amount_cents
                    if classifier_action is ConsentAction.REPROPOSE
                    else None
                ),
                latency_ms=latency_ms,
            )
        else:
            # Shadow. Recorded, compared, and then ignored.
            outcome = ConsentOutcome(
                action=_rule_action(rule_confirm),
                decided_by=DecidedBy.RULE,
                rule_confirm=rule_confirm,
                classification=classification,
                agreed=agreed,
                latency_ms=latency_ms,
            )

    if log is not None:
        log.record(
            mode=mode.value,
            rule_confirm=rule_confirm,
            threshold=threshold,
            decided_by=outcome.decided_by,
            action=outcome.action,
            session_id=session_id,
            pending_action_id=proposal.pending_action_id,
            loan_id=proposal.loan_id,
            amount_cents=proposal.amount_cents,
            reply_text=reply,
            classifier_label=(
                outcome.classification.label.value if outcome.classification else None
            ),
            classifier_confidence=(
                outcome.classification.confidence if outcome.classification else None
            ),
            modified_amount_cents=outcome.modified_amount_cents,
            agreed=outcome.agreed,
            failure=outcome.failure,
            latency_ms=outcome.latency_ms,
        )

    return outcome
