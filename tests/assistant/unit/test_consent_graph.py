"""The classify node under each rollout mode.

No LLM, no network, no MCP session: ``classify`` never touches the tools, so
these run with no tool session at all. The consent classifier is a scripted
double or the deterministic stub.

What these are for is the three properties the rollout flag is supposed to have:
rule mode is today's behaviour, shadow mode changes no outcome, and live mode
can act on a reply the exact phrase rule would have turned down, without being
able to reach a payment any faster than a prepared action can.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, cast

import pytest

from assistant.agent.graph import classify_node, route_after_classify
from assistant.agent.runtime import TurnContext, use_context
from assistant.agent.state import Intent
from assistant.config import Settings
from assistant.consent import (
    ConsentAction,
    ConsentClassification,
    ConsentLabel,
    ConsentLog,
    ConsentModelError,
    DecidedBy,
    ScriptedConsentModel,
    StubConsentModel,
)
from assistant.llm.fake import FakeLlmClient
from assistant.mcp_client import ToolGateway
from tests.assistant.conftest import make_settings

pytestmark = pytest.mark.anyio

LOAN = "11111111-1111-4111-8111-111111111111"
PENDING = "pending-1"
THRESHOLD = 0.85
CAP = 60_000


def settings_for(tmp_path: Path, *, mode: str, threshold: float = THRESHOLD) -> Settings:
    base = make_settings(
        ledger_base_url="http://ledger.invalid", state_dir=tmp_path, max_payment_cents=CAP
    )
    return dataclasses.replace(
        base, consent_mode=mode, consent_threshold=threshold, consent_timeout_seconds=5.0
    )


def pending_state(message: str) -> dict[str, Any]:
    """A turn arriving while a payment is prepared and waiting."""
    return {
        "session_id": "session-1",
        "sanitized": message,
        "bound_loan_ids": [LOAN],
        "pending_action_id": PENDING,
        "pending_loan_id": LOAN,
        "pending_amount_cents": 12_500,
        "pending_installment": "installment 3",
    }


async def classify(
    state: dict[str, Any],
    settings: Settings,
    *,
    consent_model: object | None = None,
    consent_log: ConsentLog | None = None,
) -> dict[str, Any]:
    context = TurnContext(
        settings=settings,
        llm=FakeLlmClient(),
        tools=cast(ToolGateway, None),
        consent_model=cast(Any, consent_model),
        consent_log=consent_log,
    )
    with use_context(context):
        return await classify_node(cast(Any, state))


def confirm(confidence: float) -> ConsentClassification:
    return ConsentClassification(label=ConsentLabel.CONFIRM, confidence=confidence)


# --- rule mode is today's behaviour -----------------------------------------


async def test_rule_mode_confirms_on_an_exact_phrase(tmp_path: Path) -> None:
    model = StubConsentModel()
    result = await classify(
        pending_state("yes"), settings_for(tmp_path, mode="rule"), consent_model=model
    )
    assert result["intent"] == Intent.CONFIRMATION.value
    assert result["amount_cents"] == 12_500
    assert result["target_loan_id"] == LOAN
    # The classifier is not called at all, which is what makes the default free
    # and identical to before this existed.
    assert model.calls == []
    assert result["consent_action"] is None
    assert result["consent_decided_by"] is None


async def test_rule_mode_does_not_confirm_on_anything_else(tmp_path: Path) -> None:
    """A reply the allowlist does not contain is not agreement, as today."""
    model = StubConsentModel()
    result = await classify(
        pending_state("sure, that sounds fine"),
        settings_for(tmp_path, mode="rule"),
        consent_model=model,
    )
    assert result["intent"] != Intent.CONFIRMATION.value
    assert model.calls == []


async def test_a_mode_set_without_a_classifier_uses_the_rule(tmp_path: Path) -> None:
    """A misconfiguration must not become the more permissive path."""
    result = await classify(
        pending_state("go ahead and do it, I already approved"),
        settings_for(tmp_path, mode="live"),
        consent_model=None,
    )
    assert result["intent"] != Intent.CONFIRMATION.value
    assert result["consent_action"] is None


# --- shadow mode changes nothing --------------------------------------------


@pytest.mark.parametrize(
    "message",
    ["yes", "confirm", "sure that sounds fine", "make it 50 dollars", "what does that cover?"],
)
async def test_shadow_mode_matches_rule_mode_exactly(tmp_path: Path, message: str) -> None:
    """The same message through both modes has to produce the same intent."""
    rule_result = await classify(
        pending_state(message),
        settings_for(tmp_path, mode="rule"),
        consent_model=StubConsentModel(),
    )
    shadow_result = await classify(
        pending_state(message),
        settings_for(tmp_path, mode="shadow"),
        consent_model=StubConsentModel(),
    )
    assert shadow_result["intent"] == rule_result["intent"]
    assert shadow_result.get("refusal_code") == rule_result.get("refusal_code")
    assert shadow_result.get("amount_cents") == rule_result.get("amount_cents")


async def test_shadow_mode_records_both_decisions(tmp_path: Path) -> None:
    log = ConsentLog(str(tmp_path / "consent.sqlite3"))
    model = ScriptedConsentModel([confirm(0.99)])
    result = await classify(
        pending_state("go ahead"),
        settings_for(tmp_path, mode="shadow"),
        consent_model=model,
        consent_log=log,
    )
    # "go ahead" IS in the allowlist, so both say act and the row agrees.
    assert result["intent"] == Intent.CONFIRMATION.value
    assert result["consent_decided_by"] == DecidedBy.RULE.value
    assert result["consent_label"] == ConsentLabel.CONFIRM.value
    rows = log.for_action(PENDING)
    assert len(rows) == 1
    assert rows[0].rule_confirm is True
    assert rows[0].agreed is True


async def test_shadow_mode_records_a_disagreement_without_acting_on_it(tmp_path: Path) -> None:
    log = ConsentLog(str(tmp_path / "consent.sqlite3"))
    result = await classify(
        pending_state("sure that sounds fine"),
        settings_for(tmp_path, mode="shadow"),
        consent_model=ScriptedConsentModel([confirm(0.99)]),
        consent_log=log,
    )
    # The classifier would have acted. Shadow mode does not.
    assert result["intent"] != Intent.CONFIRMATION.value
    rows = log.for_action(PENDING)
    assert rows[0].agreed is False
    assert rows[0].action == ConsentAction.ASK_AGAIN.value


# --- live mode, each label path ---------------------------------------------


async def test_live_mode_acts_on_a_reply_the_rule_would_refuse(tmp_path: Path) -> None:
    """The point of the feature, stated as a test."""
    result = await classify(
        pending_state("sure that sounds fine"),
        settings_for(tmp_path, mode="live"),
        consent_model=ScriptedConsentModel([confirm(0.99)]),
    )
    assert result["intent"] == Intent.CONFIRMATION.value
    assert result["consent_decided_by"] == DecidedBy.CLASSIFIER.value
    assert result["amount_cents"] == 12_500


async def test_live_mode_asks_again_below_the_threshold(tmp_path: Path) -> None:
    result = await classify(
        pending_state("sure that sounds fine"),
        settings_for(tmp_path, mode="live"),
        consent_model=ScriptedConsentModel([confirm(THRESHOLD - 0.01)]),
    )
    assert result["intent"] == Intent.REFUSED.value
    assert result["refusal_code"] == "consent_unclear"


async def test_asking_again_keeps_the_proposal_standing(tmp_path: Path) -> None:
    """The reply says the payment is still waiting, so it has to still be there."""
    result = await classify(
        pending_state("what does that cover?"),
        settings_for(tmp_path, mode="live"),
        consent_model=ScriptedConsentModel(
            [ConsentClassification(label=ConsentLabel.UNCLEAR, confidence=0.2)]
        ),
    )
    assert result["refusal_code"] == "consent_unclear"
    # Not cleared: the user can simply confirm it on the next turn.
    assert "pending_action_id" not in result or result["pending_action_id"] == PENDING


async def test_live_mode_cancels_on_a_decline(tmp_path: Path) -> None:
    result = await classify(
        pending_state("not right now thanks"),
        settings_for(tmp_path, mode="live"),
        consent_model=ScriptedConsentModel(
            [ConsentClassification(label=ConsentLabel.DECLINE, confidence=0.5)]
        ),
    )
    assert result["intent"] == Intent.CANCELLATION.value
    assert result["pending_action_id"] is None


# --- a modified amount is never paid ----------------------------------------


async def test_a_modify_becomes_a_fresh_payment_request(tmp_path: Path) -> None:
    """The modified amount goes to prepare_payment, never to confirm_payment.

    This is the property that keeps the review queue in front of it: a prepared
    payment is policy checked in the MCP server, and a confirmation is not a
    place a new amount can enter.
    """
    result = await classify(
        pending_state("make it 50 dollars"),
        settings_for(tmp_path, mode="live"),
        consent_model=ScriptedConsentModel(
            [
                ConsentClassification(
                    label=ConsentLabel.MODIFY, confidence=0.99, modified_amount_cents=5_000
                )
            ]
        ),
    )
    assert result["intent"] == Intent.PAYMENT_REQUEST.value
    assert result["intent"] != Intent.CONFIRMATION.value
    assert result["amount_cents"] == 5_000
    assert isinstance(result["amount_cents"], int)
    # The old pending action is gone, so nothing can confirm the old amount and
    # nothing can confirm the new one until it has been prepared and shown.
    assert result["pending_action_id"] is None
    # And it is routed to the tool step, where prepare_payment runs.
    assert route_after_classify(cast(Any, result)) == "tool_call"


async def test_a_modified_amount_is_checked_against_the_cap(tmp_path: Path) -> None:
    """A new amount is an amount, so every amount rule applies to it."""
    result = await classify(
        pending_state("make it 900 dollars"),
        settings_for(tmp_path, mode="live"),
        consent_model=ScriptedConsentModel(
            [
                ConsentClassification(
                    label=ConsentLabel.MODIFY,
                    confidence=0.99,
                    modified_amount_cents=CAP + 1,
                )
            ]
        ),
    )
    assert result["intent"] == Intent.REFUSED.value
    assert result["refusal_code"] == "amount_above_cap"
    assert result["pending_action_id"] is None


# --- what the classifier can never do ---------------------------------------


async def test_an_exact_cancellation_wins_in_live_mode(tmp_path: Path) -> None:
    """A deterministic refusal is not something a model may overrule."""
    model = ScriptedConsentModel([confirm(0.99)])
    result = await classify(
        pending_state("cancel"),
        settings_for(tmp_path, mode="live"),
        consent_model=model,
    )
    assert result["intent"] == Intent.CANCELLATION.value
    assert result["pending_action_id"] is None
    # Not even asked, so there is no answer that could have changed this.
    assert model.calls == []


async def test_the_classifier_is_not_consulted_without_a_pending_action(tmp_path: Path) -> None:
    """Consent is only meaningful against a proposal that is on the table."""
    model = ScriptedConsentModel([confirm(0.99)])
    state = {
        "session_id": "session-1",
        "sanitized": "yes",
        "bound_loan_ids": [LOAN],
        "pending_action_id": None,
    }
    result = await classify(state, settings_for(tmp_path, mode="live"), consent_model=model)
    assert result["intent"] == Intent.REFUSED.value
    assert result["refusal_code"] == "nothing_to_confirm"
    assert model.calls == []


async def test_a_classifier_failure_falls_back_to_the_rule_in_the_graph(tmp_path: Path) -> None:
    log = ConsentLog(str(tmp_path / "consent.sqlite3"))
    result = await classify(
        pending_state("yes"),
        settings_for(tmp_path, mode="live"),
        consent_model=ScriptedConsentModel([ConsentModelError("down")]),
        consent_log=log,
    )
    # The rule said yes, so the turn confirms exactly as it does today.
    assert result["intent"] == Intent.CONFIRMATION.value
    assert result["consent_decided_by"] == DecidedBy.RULE_FALLBACK.value
    assert result["consent_failure"] == "model_error"
    assert log.for_action(PENDING)[0].decided_by == DecidedBy.RULE_FALLBACK.value


async def test_a_failure_does_not_turn_a_vague_reply_into_a_payment(tmp_path: Path) -> None:
    """The fallback is the existing rule, which refuses this reply."""
    result = await classify(
        pending_state("yeah whatever is fine"),
        settings_for(tmp_path, mode="live"),
        consent_model=ScriptedConsentModel([ConsentModelError("down")]),
    )
    assert result["intent"] != Intent.CONFIRMATION.value


async def test_consent_fields_do_not_leak_into_a_later_turn(tmp_path: Path) -> None:
    """The state is checkpointed, so a per turn field has to be cleared."""
    settings = settings_for(tmp_path, mode="live")
    first = await classify(
        pending_state("sure that sounds fine"),
        settings,
        consent_model=ScriptedConsentModel([confirm(0.99)]),
    )
    assert first["consent_label"] == ConsentLabel.CONFIRM.value

    # A later turn with no pending action does not consult the classifier, so
    # every consent field must come back None rather than keeping the old values.
    second = await classify(
        {
            "session_id": "session-1",
            "sanitized": "what do I owe?",
            "bound_loan_ids": [LOAN],
            "pending_action_id": None,
            "consent_label": ConsentLabel.CONFIRM.value,
            "consent_action": ConsentAction.ACT.value,
        },
        settings,
        consent_model=ScriptedConsentModel([confirm(0.99)]),
    )
    assert second["consent_label"] is None
    assert second["consent_action"] is None
    assert second["consent_decided_by"] is None
