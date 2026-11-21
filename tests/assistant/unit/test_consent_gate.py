"""The consent schema, the decision log and the gate.

No LLM, no network, no database. The classifier is the deterministic stub or a
scripted double, so what is under test is the threshold, the fallback, the mode
and what gets recorded, not any model's judgement.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from assistant.consent import (
    ConsentAction,
    ConsentClassification,
    ConsentFailure,
    ConsentLabel,
    ConsentLog,
    ConsentMode,
    ConsentModelError,
    DecidedBy,
    Proposal,
    ScriptedConsentModel,
    StubConsentModel,
    decide_consent,
)
from assistant.consent.stub import HIGH_CONFIDENCE, LOW_CONFIDENCE

# The project's async convention: anyio, with the backend fixture in conftest.
# Harmless on the synchronous tests in this module, which anyio leaves alone.
pytestmark = pytest.mark.anyio

THRESHOLD = 0.85
PROPOSAL = Proposal(
    amount_cents=12_500,
    payee="loan 7f3a",
    installment="installment 3",
    pending_action_id="pending-1",
    loan_id="11111111-1111-4111-8111-111111111111",
)


def confirm(confidence: float) -> ConsentClassification:
    return ConsentClassification(label=ConsentLabel.CONFIRM, confidence=confidence)


async def run(
    *,
    mode: ConsentMode,
    reply: str,
    model: object,
    rule_confirm: bool,
    log: ConsentLog | None = None,
    threshold: float = THRESHOLD,
    timeout_seconds: float = 5.0,
):
    return await decide_consent(
        mode=mode,
        reply=reply,
        proposal=PROPOSAL,
        model=model,  # type: ignore[arg-type]
        rule_confirm=rule_confirm,
        threshold=threshold,
        timeout_seconds=timeout_seconds,
        log=log,
        session_id="session-1",
    )


# --- the schema is the contract ---------------------------------------------


def test_a_modify_carries_integer_cents() -> None:
    decision = ConsentClassification(
        label=ConsentLabel.MODIFY, confidence=0.9, modified_amount_cents=5_000
    )
    assert decision.modified_amount_cents == 5_000
    assert isinstance(decision.modified_amount_cents, int)


@pytest.mark.parametrize("amount", [5_000.0, "5000", True, 5_000.5])
def test_a_modified_amount_that_is_not_an_integer_is_refused(amount: object) -> None:
    """Money is integer cents everywhere in this codebase or it is nothing.

    Pydantic's ordinary coercion would accept 5000.0 and True for an int field,
    which is why the field is a StrictInt.
    """
    with pytest.raises(ValidationError):
        ConsentClassification(
            label=ConsentLabel.MODIFY, confidence=0.9, modified_amount_cents=amount
        )


def test_a_modify_without_an_amount_is_refused() -> None:
    with pytest.raises(ValidationError):
        ConsentClassification(label=ConsentLabel.MODIFY, confidence=0.9)


@pytest.mark.parametrize("amount", [0, -1])
def test_a_modify_amount_must_be_positive(amount: int) -> None:
    with pytest.raises(ValidationError):
        ConsentClassification(
            label=ConsentLabel.MODIFY, confidence=0.9, modified_amount_cents=amount
        )


@pytest.mark.parametrize(
    "label", [ConsentLabel.CONFIRM, ConsentLabel.DECLINE, ConsentLabel.UNCLEAR]
)
def test_only_a_modify_may_carry_an_amount(label: ConsentLabel) -> None:
    """An amount on any other label would be a value nothing reads."""
    with pytest.raises(ValidationError):
        ConsentClassification(label=label, confidence=0.9, modified_amount_cents=100)


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_confidence_has_to_be_a_probability(confidence: float) -> None:
    with pytest.raises(ValidationError):
        ConsentClassification(label=ConsentLabel.CONFIRM, confidence=confidence)


def test_an_invented_field_is_refused() -> None:
    """A model that answers a different question than the one asked is unclear."""
    with pytest.raises(ValidationError):
        ConsentClassification.model_validate(
            {"label": "confirm", "confidence": 0.9, "reasoning": "they said yes"}
        )


def test_an_unknown_label_is_refused() -> None:
    with pytest.raises(ValidationError):
        ConsentClassification.model_validate({"label": "probably", "confidence": 0.9})


# --- each label path --------------------------------------------------------


async def test_a_confident_confirm_acts() -> None:
    outcome = await run(
        mode=ConsentMode.LIVE,
        reply="yes",
        model=ScriptedConsentModel([confirm(0.99)]),
        rule_confirm=True,
    )
    assert outcome.action is ConsentAction.ACT
    assert outcome.decided_by is DecidedBy.CLASSIFIER


async def test_a_confirm_below_the_threshold_asks_again() -> None:
    """The threshold is the whole point: the label alone does not act."""
    outcome = await run(
        mode=ConsentMode.LIVE,
        reply="yes I think so",
        model=ScriptedConsentModel([confirm(THRESHOLD - 0.01)]),
        rule_confirm=False,
    )
    assert outcome.action is ConsentAction.ASK_AGAIN
    assert outcome.decided_by is DecidedBy.CLASSIFIER


async def test_the_threshold_is_inclusive_at_its_own_value() -> None:
    outcome = await run(
        mode=ConsentMode.LIVE,
        reply="yes",
        model=ScriptedConsentModel([confirm(THRESHOLD)]),
        rule_confirm=True,
    )
    assert outcome.action is ConsentAction.ACT


async def test_a_decline_cancels() -> None:
    outcome = await run(
        mode=ConsentMode.LIVE,
        reply="no, not now",
        model=ScriptedConsentModel(
            [ConsentClassification(label=ConsentLabel.DECLINE, confidence=0.4)]
        ),
        rule_confirm=False,
    )
    assert outcome.action is ConsentAction.CANCEL


async def test_a_decline_cancels_whatever_its_confidence() -> None:
    """The threshold guards acting, not refusing. A hesitant decline still cancels."""
    outcome = await run(
        mode=ConsentMode.LIVE,
        reply="hmm, no",
        model=ScriptedConsentModel(
            [ConsentClassification(label=ConsentLabel.DECLINE, confidence=0.01)]
        ),
        rule_confirm=False,
    )
    assert outcome.action is ConsentAction.CANCEL


async def test_a_modify_reproposes_and_carries_the_amount() -> None:
    outcome = await run(
        mode=ConsentMode.LIVE,
        reply="make it 50 dollars",
        model=ScriptedConsentModel(
            [
                ConsentClassification(
                    label=ConsentLabel.MODIFY, confidence=0.95, modified_amount_cents=5_000
                )
            ]
        ),
        rule_confirm=False,
    )
    assert outcome.action is ConsentAction.REPROPOSE
    assert outcome.modified_amount_cents == 5_000


async def test_an_unclear_reply_asks_again() -> None:
    outcome = await run(
        mode=ConsentMode.LIVE,
        reply="what does that cover?",
        model=ScriptedConsentModel(
            [ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=0.2)]
        ),
        rule_confirm=False,
    )
    assert outcome.action is ConsentAction.ASK_AGAIN


async def test_only_a_repropose_reports_a_modified_amount() -> None:
    """So that nothing downstream can read an amount off a decision that is not
    a reproposal."""
    outcome = await run(
        mode=ConsentMode.LIVE,
        reply="yes",
        model=ScriptedConsentModel([confirm(0.99)]),
        rule_confirm=True,
    )
    assert outcome.modified_amount_cents is None


# --- the fallback -----------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected_failure"),
    [
        (ScriptedConsentModel([ConsentModelError("down")]), ConsentFailure.MODEL_ERROR),
        (ScriptedConsentModel([RuntimeError("something else")]), ConsentFailure.MODEL_ERROR),
    ],
)
async def test_a_model_failure_falls_back_to_the_rule(
    model: ScriptedConsentModel, expected_failure: ConsentFailure
) -> None:
    outcome = await run(mode=ConsentMode.LIVE, reply="yes", model=model, rule_confirm=True)
    assert outcome.decided_by is DecidedBy.RULE_FALLBACK
    assert outcome.failure is expected_failure
    # The rule said this was a confirmation, so the fallback acts exactly as the
    # assistant does today.
    assert outcome.action is ConsentAction.ACT


async def test_a_timeout_falls_back_to_the_rule() -> None:
    outcome = await run(
        mode=ConsentMode.LIVE,
        reply="yes",
        model=ScriptedConsentModel([confirm(0.99)], hang_seconds=5.0),
        rule_confirm=True,
        timeout_seconds=0.01,
    )
    assert outcome.failure is ConsentFailure.TIMEOUT
    assert outcome.decided_by is DecidedBy.RULE_FALLBACK
    assert outcome.action is ConsentAction.ACT


async def test_the_fallback_does_not_act_on_a_reply_the_rule_rejects() -> None:
    """The fallback is the existing rule, so a reply that is not an exact phrase
    is not agreement, exactly as today."""
    outcome = await run(
        mode=ConsentMode.LIVE,
        reply="sure, sounds fine",
        model=ScriptedConsentModel([ConsentModelError("down")]),
        rule_confirm=False,
    )
    assert outcome.decided_by is DecidedBy.RULE_FALLBACK
    assert outcome.action is ConsentAction.ASK_AGAIN


async def test_an_answer_that_fails_validation_never_reaches_the_gate() -> None:
    """The schema stops it at the model boundary, so the gate sees a failure.

    This is the invalid output path: the model answered, the answer did not fit,
    and the decision falls back rather than being half believed.
    """
    model = ScriptedConsentModel([ConsentModelError("did not validate")])
    outcome = await run(mode=ConsentMode.LIVE, reply="yes", model=model, rule_confirm=True)
    assert outcome.classification is None
    assert outcome.decided_by is DecidedBy.RULE_FALLBACK


# --- shadow mode ------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "rule_confirm", "expected"),
    [
        ("yes", True, ConsentAction.ACT),
        ("sure, go on then", False, ConsentAction.ASK_AGAIN),
        ("make it 50 dollars", False, ConsentAction.ASK_AGAIN),
        ("no", False, ConsentAction.ASK_AGAIN),
    ],
)
async def test_shadow_mode_always_returns_the_rules_outcome(
    reply: str, rule_confirm: bool, expected: ConsentAction
) -> None:
    """Whatever the classifier says, shadow mode does what the rule says."""
    outcome = await run(
        mode=ConsentMode.SHADOW,
        reply=reply,
        model=StubConsentModel(),
        rule_confirm=rule_confirm,
    )
    assert outcome.action is expected
    assert outcome.decided_by is DecidedBy.RULE


async def test_shadow_mode_still_runs_and_records_the_classifier() -> None:
    model = StubConsentModel()
    outcome = await run(
        mode=ConsentMode.SHADOW, reply="make it 50 dollars", model=model, rule_confirm=False
    )
    assert model.calls == ["make it 50 dollars"]
    assert outcome.classification is not None
    assert outcome.classification.label is ConsentLabel.MODIFY
    # Recorded, compared, and then not acted on.
    assert outcome.action is ConsentAction.ASK_AGAIN


# --- the log ----------------------------------------------------------------


def test_the_log_is_append_only(tmp_path: Path) -> None:
    log = ConsentLog(str(tmp_path / "consent.sqlite3"))
    record = log.record(
        mode="shadow",
        rule_confirm=True,
        threshold=THRESHOLD,
        decided_by=DecidedBy.RULE,
        action=ConsentAction.ACT,
        pending_action_id="pending-1",
    )
    with sqlite3.connect(str(tmp_path / "consent.sqlite3")) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE consent_decisions SET action = 'cancel' WHERE id = ?", (record.id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM consent_decisions WHERE id = ?", (record.id,))


async def test_shadow_mode_logs_both_decisions_and_whether_they_agree(tmp_path: Path) -> None:
    log = ConsentLog(str(tmp_path / "consent.sqlite3"))
    # The rule says no (not an exact phrase), the stub says confirm with high
    # confidence, so the two disagree and the row has to say so.
    await run(
        mode=ConsentMode.SHADOW,
        reply="go ahead",
        model=ScriptedConsentModel([confirm(HIGH_CONFIDENCE)]),
        rule_confirm=False,
        log=log,
    )
    rows = log.for_action("pending-1")
    assert len(rows) == 1
    row = rows[0]
    assert row.mode == "shadow"
    assert row.rule_confirm is False
    assert row.classifier_label == ConsentLabel.CONFIRM.value
    assert row.classifier_confidence == HIGH_CONFIDENCE
    assert row.agreed is False
    assert row.decided_by == DecidedBy.RULE.value
    assert row.action == ConsentAction.ASK_AGAIN.value
    assert row.threshold == THRESHOLD
    assert row.reply_text == "go ahead"


async def test_agreement_is_recorded_when_the_two_decisions_match(tmp_path: Path) -> None:
    log = ConsentLog(str(tmp_path / "consent.sqlite3"))
    await run(
        mode=ConsentMode.SHADOW,
        reply="yes",
        model=ScriptedConsentModel([confirm(HIGH_CONFIDENCE)]),
        rule_confirm=True,
        log=log,
    )
    await run(
        mode=ConsentMode.SHADOW,
        reply="what does that cover?",
        model=ScriptedConsentModel(
            [ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=LOW_CONFIDENCE)]
        ),
        rule_confirm=False,
        log=log,
    )
    counts = log.agreement_counts()
    assert counts == {"agreed": 2, "disagreed": 0, "not_compared": 0, "total": 2}


async def test_a_fallback_is_logged_as_not_compared(tmp_path: Path) -> None:
    """There is nothing to compare when the classifier never answered, and
    counting that as agreement would flatter the comparison."""
    log = ConsentLog(str(tmp_path / "consent.sqlite3"))
    await run(
        mode=ConsentMode.LIVE,
        reply="yes",
        model=ScriptedConsentModel([ConsentModelError("down")]),
        rule_confirm=True,
        log=log,
    )
    counts = log.agreement_counts()
    assert counts["not_compared"] == 1
    assert counts["agreed"] == 0
    row = log.for_action("pending-1")[0]
    assert row.failure == ConsentFailure.MODEL_ERROR.value
    assert row.decided_by == DecidedBy.RULE_FALLBACK.value
    assert row.classifier_label is None


async def test_every_decision_is_logged_with_the_proposal_it_was_about(tmp_path: Path) -> None:
    log = ConsentLog(str(tmp_path / "consent.sqlite3"))
    await run(
        mode=ConsentMode.LIVE,
        reply="yes",
        model=ScriptedConsentModel([confirm(0.99)]),
        rule_confirm=True,
        log=log,
    )
    row = log.recent()[0]
    assert row.amount_cents == PROPOSAL.amount_cents
    assert row.loan_id == PROPOSAL.loan_id
    assert row.session_id == "session-1"
    assert row.latency_ms is not None


# --- the stub itself --------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "label"),
    [
        ("yes", ConsentLabel.CONFIRM),
        ("no", ConsentLabel.DECLINE),
        ("cancel that", ConsentLabel.DECLINE),
        ("make it 50 dollars", ConsentLabel.MODIFY),
        ("yes but only 25 dollars", ConsentLabel.MODIFY),
        ("does that clear it?", ConsentLabel.UNCLEAR),
        ("thanks", ConsentLabel.UNCLEAR),
        ("", ConsentLabel.UNCLEAR),
    ],
)
def test_the_stub_labels_the_obvious_cases(reply: str, label: ConsentLabel) -> None:
    assert StubConsentModel().decide(reply).label is label


def test_the_stub_reads_a_modified_amount_as_integer_cents() -> None:
    assert StubConsentModel().decide("make it 50 dollars").modified_amount_cents == 5_000
    assert StubConsentModel().decide("change it to 2500 cents").modified_amount_cents == 2_500


def test_the_stub_refuses_to_guess_an_amount_it_cannot_read() -> None:
    """Wanting a change without saying what to is unclear, not a modify."""
    decision = StubConsentModel().decide("make it less")
    assert decision.label is ConsentLabel.UNCLEAR
