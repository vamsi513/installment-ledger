"""Live consent mode end to end.

Real Postgres, a real ledger over loopback, a real MCP subprocess and the real
review queue. Only the consent classifier is a double, because the point is to
exercise everything downstream of its decision: the threshold, the fallback, the
review gate and the append only trails.

Live mode is switched on through settings rather than the environment, so these
tests cannot leak the mode into any other test.

What makes these worth having over the unit tests: in live mode the agent acts
on a reply the exact phrase rule would refuse, so every one of these payments is
one the committed rule mode invariant would have called illegitimate. The thing
that has to hold instead is that a payment traces to a consent decision that
permitted it, and that the review queue still sits in front of it.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest
from sqlalchemy import Engine, text

from assistant.agent import run_turn
from assistant.agent.state import Intent, Outcome
from assistant.config import Settings
from assistant.consent import (
    ConsentAction,
    ConsentClassification,
    ConsentLabel,
    ConsentLog,
    ConsentModelError,
    DecidedBy,
    ScriptedConsentModel,
    consent_log_path_for,
)
from assistant.mcp_client import ToolGateway
from assistant.pending import ActionStatus
from assistant.review import ReviewService
from assistant.review.audit import AuditAction
from tests.assistant.conftest import TEST_REVIEWER_ID
from tests.helpers import create_loan

pytestmark = pytest.mark.anyio

THRESHOLD = 0.85
#: Under the review threshold of 40000, so it is an ordinary payment.
NORMAL_CENTS = 30_000
#: Over it, so the policy holds it.
RISKY_CENTS = 50_000

#: Not an exact confirmation phrase, so the committed rule refuses it and only
#: the classifier can turn it into a payment. That is the whole point.
VAGUE_YES = "sure that sounds fine"


def payments(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT COUNT(*) FROM payments")).scalar_one())


def paid_cents(engine: Engine, loan_id: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE loan_id = :id"),
                {"id": loan_id},
            ).scalar_one()
        )


def live(settings: Settings, *, timeout_seconds: float = 5.0) -> Settings:
    return dataclasses.replace(
        settings,
        consent_mode="live",
        consent_threshold=THRESHOLD,
        consent_timeout_seconds=timeout_seconds,
    )


def confirms(count: int, confidence: float = 0.97) -> list[ConsentClassification]:
    return [
        ConsentClassification(label=ConsentLabel.CONFIRM, confidence=confidence)
        for _ in range(count)
    ]


async def drive(
    settings: Settings,
    loan_id: str,
    messages: list[str],
    model: ScriptedConsentModel,
    log: ConsentLog,
    *,
    session_id: str = "live-session",
) -> list[object]:
    """Run several turns through the real graph with the scripted classifier."""
    results = []
    for message in messages:
        results.append(
            await run_turn(
                session_id=session_id,
                message=message,
                bound_loan_ids=[loan_id],
                settings=settings,
                consent_model=model,
                consent_log=log,
            )
        )
    return results


def consent_log_for(settings: Settings) -> ConsentLog:
    return ConsentLog(consent_log_path_for(settings))


def audit_actions(service: ReviewService, loan_id: str) -> list[str]:
    return [entry.action.value for entry in service.audit.entries(loan_id=loan_id, limit=200)]


def held_action_id(service: ReviewService, loan_id: str) -> str:
    for entry in service.audit.entries(loan_id=loan_id, limit=200):
        if entry.action is AuditAction.HELD_FOR_REVIEW and entry.pending_action_id:
            return entry.pending_action_id
    raise AssertionError("nothing was held for review")


# --- a confident confirm on an ordinary payment -----------------------------


async def test_a_confident_confirm_posts_exactly_once(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    settings = live(assistant_settings)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    before = payments(engine)

    results = await drive(
        settings,
        loan["id"],
        [f"pay {NORMAL_CENTS} cents", VAGUE_YES],
        ScriptedConsentModel(confirms(1)),
        log,
    )

    assert results[1].outcome == Outcome.APPLIED.value  # type: ignore[attr-defined]
    assert payments(engine) == before + 1
    assert paid_cents(engine, loan["id"]) == NORMAL_CENTS

    rows = log.recent(limit=10)
    acting = [row for row in rows if row.action == ConsentAction.ACT.value]
    assert len(acting) == 1
    assert acting[0].decided_by == DecidedBy.CLASSIFIER.value
    assert acting[0].classifier_label == ConsentLabel.CONFIRM.value
    assert acting[0].classifier_confidence is not None
    assert acting[0].classifier_confidence >= THRESHOLD
    # The reply would not have passed the committed rule, which is what makes
    # this a live mode payment rather than an ordinary one.
    assert acting[0].rule_confirm is False


async def test_a_confirm_below_the_threshold_posts_nothing(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    settings = live(assistant_settings)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    before = payments(engine)

    results = await drive(
        settings,
        loan["id"],
        [f"pay {NORMAL_CENTS} cents", VAGUE_YES],
        ScriptedConsentModel(confirms(1, confidence=THRESHOLD - 0.01)),
        log,
    )

    assert results[1].refusal_code == "consent_unclear"  # type: ignore[attr-defined]
    assert payments(engine) == before
    assert log.recent(limit=5)[0].action == ConsentAction.ASK_AGAIN.value


# --- the review queue is still in front -------------------------------------


async def test_a_risky_payment_is_held_and_does_not_post_until_approved(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    """The classifier cannot buy its way past review.

    It confirms with high confidence, twice, and the payment still does not
    reach the ledger until a reviewer releases it.
    """
    settings = live(review_settings)
    log = consent_log_for(settings)
    service = ReviewService(settings)
    loan = create_loan(client, principal_cents=200_000, installment_count=4)
    before = payments(engine)

    results = await drive(
        settings,
        loan["id"],
        [f"pay {RISKY_CENTS} cents", VAGUE_YES],
        ScriptedConsentModel(confirms(2)),
        log,
    )

    # The classifier said yes and the queue said not yet.
    assert results[1].outcome != Outcome.APPLIED.value  # type: ignore[attr-defined]
    assert payments(engine) == before
    actions = audit_actions(service, loan["id"])
    assert AuditAction.HELD_FOR_REVIEW.value in actions
    assert AuditAction.CONFIRMED.value not in actions

    acting = [row for row in log.recent(limit=10) if row.action == ConsentAction.ACT.value]
    assert acting, "the classifier did authorise acting; the queue is what stopped it"

    pending_action_id = held_action_id(service, loan["id"])
    approval = service.approve(pending_action_id, actor=TEST_REVIEWER_ID)
    assert approval.changed

    async with ToolGateway(settings) as tools:
        outcome = await tools.call("confirm_payment", {"pending_action_id": pending_action_id})

    assert outcome.status == "ok"
    assert payments(engine) == before + 1
    actions = audit_actions(service, loan["id"])
    assert actions.index(AuditAction.APPROVED.value) < actions.index(AuditAction.CONFIRMED.value)


async def test_a_rejected_risky_payment_never_posts(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    settings = live(review_settings)
    log = consent_log_for(settings)
    service = ReviewService(settings)
    loan = create_loan(client, principal_cents=200_000, installment_count=4)
    before = payments(engine)

    await drive(
        settings,
        loan["id"],
        [f"pay {RISKY_CENTS} cents", VAGUE_YES],
        ScriptedConsentModel(confirms(2)),
        log,
    )
    pending_action_id = held_action_id(service, loan["id"])
    service.reject(pending_action_id, actor=TEST_REVIEWER_ID, reason="not this one")

    async with ToolGateway(settings) as tools:
        outcome = await tools.call("confirm_payment", {"pending_action_id": pending_action_id})

    assert outcome.status == "rejected"
    assert payments(engine) == before
    assert AuditAction.CONFIRMED.value not in audit_actions(service, loan["id"])


# --- a modified amount is never the one that posts --------------------------


async def test_a_modify_reproposes_and_never_posts_the_modified_amount(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """The reproposed amount has to be prepared and confirmed again.

    Three turns: ask to pay, ask to change it, then agree. What posts is the
    modified amount, but only after it was put back to the user as a fresh
    proposal, so there is a second consent decision authorising it.
    """
    settings = live(assistant_settings)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    before = payments(engine)
    modified = 5_000

    model = ScriptedConsentModel(
        [
            ConsentClassification(
                label=ConsentLabel.MODIFY, confidence=0.99, modified_amount_cents=modified
            ),
            ConsentClassification(label=ConsentLabel.CONFIRM, confidence=0.97),
        ]
    )
    # Driven in two stages on purpose: a claim about what had not happened yet
    # has to be checked before the later turns run.
    session = "live-modify"
    asked = await drive(
        settings,
        loan["id"],
        [f"pay {NORMAL_CENTS} cents", "make it 50 dollars"],
        model,
        log,
        session_id=session,
    )

    # The turn that asked for a change prepared a new payment; it did not pay.
    assert "prepare_payment" in asked[1].tool_calls  # type: ignore[attr-defined]
    assert "confirm_payment" not in asked[1].tool_calls  # type: ignore[attr-defined]
    assert payments(engine) == before

    agreed = await drive(settings, loan["id"], [VAGUE_YES], model, log, session_id=session)

    # Only now does it pay, and it pays the modified amount.
    assert agreed[0].outcome == Outcome.APPLIED.value  # type: ignore[attr-defined]
    assert payments(engine) == before + 1
    assert paid_cents(engine, loan["id"]) == modified

    rows = log.recent(limit=10)
    reproposed = [row for row in rows if row.action == ConsentAction.REPROPOSE.value]
    acted = [row for row in rows if row.action == ConsentAction.ACT.value]
    assert len(reproposed) == 1
    assert reproposed[0].modified_amount_cents == modified
    assert len(acted) == 1
    # The decision that paid is about a different proposal than the one that was
    # reproposed, which is what "a new confirmation" means here.
    assert acted[0].pending_action_id != reproposed[0].pending_action_id


async def test_a_modified_amount_over_the_cap_posts_nothing(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """A new amount is an amount, so the cap applies to it."""
    settings = live(assistant_settings)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=500_000, installment_count=4)
    before = payments(engine)

    results = await drive(
        settings,
        loan["id"],
        [f"pay {NORMAL_CENTS} cents", "make it enormous"],
        ScriptedConsentModel(
            [
                ConsentClassification(
                    label=ConsentLabel.MODIFY,
                    confidence=0.99,
                    modified_amount_cents=settings.max_payment_cents + 1,
                )
            ]
        ),
        log,
    )

    assert results[1].refusal_code == "amount_above_cap"  # type: ignore[attr-defined]
    assert payments(engine) == before


# --- the fallback -----------------------------------------------------------


async def test_a_timeout_falls_back_to_the_exact_rule_and_posts(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """The classifier hangs, so the existing rule decides. "yes" is an exact
    phrase, so the payment goes through exactly as it does in rule mode."""
    settings = live(assistant_settings, timeout_seconds=0.05)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    before = payments(engine)

    results = await drive(
        settings,
        loan["id"],
        [f"pay {NORMAL_CENTS} cents", "yes"],
        ScriptedConsentModel(confirms(1), hang_seconds=5.0),
        log,
    )

    assert results[1].outcome == Outcome.APPLIED.value  # type: ignore[attr-defined]
    assert payments(engine) == before + 1
    row = log.recent(limit=5)[0]
    assert row.decided_by == DecidedBy.RULE_FALLBACK.value
    assert row.failure == "timeout"
    assert row.rule_confirm is True
    assert row.classifier_label is None


async def test_a_timeout_on_a_vague_reply_posts_nothing(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """The fallback is the existing rule, which refuses this reply. So a
    classifier outage cannot be used to get a vague reply treated as consent."""
    settings = live(assistant_settings, timeout_seconds=0.05)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    before = payments(engine)

    await drive(
        settings,
        loan["id"],
        [f"pay {NORMAL_CENTS} cents", VAGUE_YES],
        ScriptedConsentModel(confirms(1), hang_seconds=5.0),
        log,
    )

    assert payments(engine) == before
    row = log.recent(limit=5)[0]
    assert row.decided_by == DecidedBy.RULE_FALLBACK.value
    assert row.failure == "timeout"
    assert row.rule_confirm is False


async def test_an_api_error_falls_back_to_the_exact_rule(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    settings = live(assistant_settings)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    before = payments(engine)

    results = await drive(
        settings,
        loan["id"],
        [f"pay {NORMAL_CENTS} cents", "yes"],
        ScriptedConsentModel([ConsentModelError("the model is down")]),
        log,
    )

    assert results[1].outcome == Outcome.APPLIED.value  # type: ignore[attr-defined]
    assert payments(engine) == before + 1
    row = log.recent(limit=5)[0]
    assert row.decided_by == DecidedBy.RULE_FALLBACK.value
    assert row.failure == "model_error"


# --- the trails -------------------------------------------------------------


async def test_the_trails_record_which_path_decided_each_time(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    """Every turn that reached a consent decision left a row saying who decided.

    One loan, three decisions, one of each path that can occur in live mode:
    the classifier acting, the classifier declining to act, and the fallback.
    """
    settings = live(review_settings)
    log = consent_log_for(settings)
    service = ReviewService(settings)
    loan = create_loan(client, principal_cents=200_000, installment_count=6)

    model = ScriptedConsentModel(
        [
            ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=0.2),
            ConsentModelError("down"),
            ConsentClassification(label=ConsentLabel.CONFIRM, confidence=0.97),
        ]
    )
    second = 10_000
    await drive(
        settings,
        loan["id"],
        [
            f"pay {NORMAL_CENTS} cents",
            "hmm not sure",  # classifier says unclear, asks again, proposal lives
            "yes",  # classifier errors, the exact rule decides, pays
            f"pay {second} cents",  # a fresh proposal, no decision of its own
            VAGUE_YES,  # classifier confirms, pays
        ],
        model,
        log,
    )

    rows = list(reversed(log.recent(limit=10)))
    assert [row.decided_by for row in rows] == [
        DecidedBy.CLASSIFIER.value,
        DecidedBy.RULE_FALLBACK.value,
        DecidedBy.CLASSIFIER.value,
    ]
    assert [row.action for row in rows] == [
        ConsentAction.ASK_AGAIN.value,
        ConsentAction.ACT.value,
        ConsentAction.ACT.value,
    ]
    assert rows[1].failure == "timeout" or rows[1].failure == "model_error"
    # Every row names the proposal it judged and the threshold in force.
    for row in rows:
        assert row.pending_action_id
        assert row.loan_id == loan["id"]
        assert row.threshold == THRESHOLD
    assert [row.amount_cents for row in rows] == [NORMAL_CENTS, NORMAL_CENTS, second]
    # The two paid proposals are different ones, judged separately.
    assert rows[1].pending_action_id != rows[2].pending_action_id

    # And the review queue's own trail is unchanged in shape by any of it.
    assert AuditAction.PREPARED.value in audit_actions(service, loan["id"])


async def test_the_consent_log_is_append_only_in_a_real_run(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """The decisions cannot be rewritten after the fact, enforced by the
    database rather than by convention."""
    import sqlite3

    settings = live(assistant_settings)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    await drive(
        settings,
        loan["id"],
        [f"pay {NORMAL_CENTS} cents", VAGUE_YES],
        ScriptedConsentModel(confirms(1)),
        log,
    )
    row = log.recent(limit=1)[0]

    with sqlite3.connect(consent_log_path_for(settings)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE consent_decisions SET action = 'cancel' WHERE id = ?", (row.id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM consent_decisions WHERE id = ?", (row.id,))


# --- rule mode is untouched by any of this ----------------------------------


async def test_rule_mode_still_refuses_the_vague_reply(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """The same conversation, with the default mode, posts nothing.

    This is the control for every test above: what makes them live mode tests is
    that this one does not pay.
    """
    before = payments(engine)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)

    for message in [f"pay {NORMAL_CENTS} cents", VAGUE_YES]:
        await run_turn(
            session_id="rule-session",
            message=message,
            bound_loan_ids=[loan["id"]],
            settings=assistant_settings,
        )

    assert payments(engine) == before
    assert assistant_settings.consent_mode == "rule"


async def test_a_held_action_is_still_held_in_rule_mode(
    client: httpx.Client, review_settings: Settings, engine: Engine
) -> None:
    """And the review queue behaves the same with the classifier switched off."""
    before = payments(engine)
    loan = create_loan(client, principal_cents=200_000, installment_count=4)

    async with ToolGateway(review_settings) as tools:
        preview = await tools.call(
            "prepare_payment", {"loan_id": loan["id"], "amount_cents": RISKY_CENTS}
        )
        assert preview.payload["review_status"] == ActionStatus.NEEDS_REVIEW.value
        outcome = await tools.call(
            "confirm_payment", {"pending_action_id": preview.payload["pending_action_id"]}
        )

    assert outcome.status == "rejected"
    assert payments(engine) == before


# --- proposal lifetime ------------------------------------------------------


async def test_a_change_of_subject_cancels_the_pending_payment(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """The regression. Live mode used to pay here and rule mode never did.

    The middle turn is a question, not a reply to the proposal. The classifier
    reads it as unclear, which used to mean "ask again, proposal still live", so
    the "yes" that followed paid a proposal the user had already moved on from.
    It is handled as the question it is now, which cancels the payment.
    """
    settings = live(assistant_settings)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    before = payments(engine)

    results = await drive(
        settings,
        loan["id"],
        ["Pay 300 dollars.", "Actually, what is my balance?", "yes"],
        ScriptedConsentModel([ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=0.2)]),
        log,
        session_id="live-topic-change",
    )

    # The question is answered as a question.
    assert results[1].intent == Intent.READ_QUESTION.value  # type: ignore[attr-defined]
    assert "get_loan_summary" in results[1].tool_calls  # type: ignore[attr-defined]
    # And the proposal is gone, so there is nothing for the "yes" to confirm.
    assert results[1].has_pending_action is False  # type: ignore[attr-defined]
    assert results[2].refusal_code == "nothing_to_confirm"  # type: ignore[attr-defined]
    assert payments(engine) == before


async def test_a_fresh_payment_request_reprepares(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """A new request while a proposal is pending is a request, not a reply.

    Same as rule mode: it prepares again and pays nothing by itself.
    """
    settings = live(assistant_settings)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    before = payments(engine)

    results = await drive(
        settings,
        loan["id"],
        ["Pay 100 dollars.", "Pay 100 dollars."],
        ScriptedConsentModel([ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=0.2)]),
        log,
        session_id="live-repeat-request",
    )

    assert results[1].intent == Intent.PAYMENT_REQUEST.value  # type: ignore[attr-defined]
    assert "prepare_payment" in results[1].tool_calls  # type: ignore[attr-defined]
    assert "confirm_payment" not in results[1].tool_calls  # type: ignore[attr-defined]
    assert results[1].has_pending_action is True  # type: ignore[attr-defined]
    assert payments(engine) == before


async def test_a_vague_reply_about_the_proposal_still_asks_again(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """The case the ask again path is actually for.

    Nothing recognises "hmm not sure" as a question or a request, so it is a
    vague reply about the proposal: ask again, pay nothing, and leave the
    proposal standing so a following "yes" can still confirm it.
    """
    settings = live(assistant_settings)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    before = payments(engine)

    # Two stages, because the claim is about what had not happened yet.
    session = "live-vague-reply"
    model = ScriptedConsentModel(
        [
            ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=0.2),
            ConsentClassification(label=ConsentLabel.CONFIRM, confidence=0.97),
        ]
    )
    asked = await drive(
        settings,
        loan["id"],
        ["Pay 300 dollars.", "hmm not sure"],
        model,
        log,
        session_id=session,
    )

    assert asked[1].refusal_code == "consent_unclear"  # type: ignore[attr-defined]
    assert asked[1].has_pending_action is True  # type: ignore[attr-defined]
    assert payments(engine) == before

    # The proposal survived a reply that was about it, so a yes still works.
    agreed = await drive(settings, loan["id"], ["yes"], model, log, session_id=session)
    assert agreed[0].outcome == Outcome.APPLIED.value  # type: ignore[attr-defined]
    assert payments(engine) == before + 1
    assert paid_cents(engine, loan["id"]) == 30_000


async def test_an_out_of_scope_request_still_cancels_the_proposal(
    client: httpx.Client, assistant_settings: Settings, engine: Engine
) -> None:
    """A recognised refusal is a change of subject, not a vague reply.

    This is the other side of the fix: only a message nothing recognised is
    treated as a reply about the proposal. Something the scope patterns catch
    cancels the payment, as it does in rule mode.
    """
    settings = live(assistant_settings)
    log = consent_log_for(settings)
    loan = create_loan(client, principal_cents=120_000, installment_count=4)
    before = payments(engine)

    results = await drive(
        settings,
        loan["id"],
        ["Pay 300 dollars.", "Should I refinance this loan?", "yes"],
        ScriptedConsentModel([ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=0.2)]),
        log,
        session_id="live-out-of-scope",
    )

    assert results[1].intent == Intent.OUT_OF_SCOPE.value  # type: ignore[attr-defined]
    assert results[1].refusal_code == "financial_advice"  # type: ignore[attr-defined]
    assert results[1].has_pending_action is False  # type: ignore[attr-defined]
    assert results[2].refusal_code == "nothing_to_confirm"  # type: ignore[attr-defined]
    assert payments(engine) == before
