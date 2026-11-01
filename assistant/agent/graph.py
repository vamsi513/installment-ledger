"""The LangGraph graph: sanitize, classify, tool call, respond.

Where each safety rule lives:

* ``sanitize`` folds unicode, drops control characters and truncates.
* ``classify`` resolves cancellation, confirmation and out of scope with code,
  before any model call. Only what is left over costs a model call, and the
  model returns a label and an amount, never a tool choice. Every intent except
  a confirmation clears the pending action, which is what makes a change of
  subject cancel a pending payment.
* ``tool_call`` is reached only for an intent that survived the guards. It is
  the only place that touches MCP.
* ``respond`` fills a template from tool output. No model call, so a reply
  cannot contain a number the ledger did not return.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from ..llm import LlmUnavailable, Message
from ..mcp_client import ToolTransportError
from ..redaction import redact_loan_id, redact_session_id
from . import replies
from .guards import (
    check_amount,
    is_explicit_cancellation,
    is_explicit_confirmation,
    looks_like_injection,
    mentions_negative_amount,
    out_of_scope_reason,
    resolve_target_loan,
    sanitize_message,
)
from .prompts import build_classifier_messages
from .runtime import current_context
from .state import AgentState, Intent, Outcome

logger = logging.getLogger("assistant.agent")

LEDGER_LINE_LIMIT = 20
ALLOWED_INTENTS = {
    Intent.READ_QUESTION.value,
    Intent.PAYMENT_REQUEST.value,
    Intent.OUT_OF_SCOPE.value,
}

_CLEARED_PENDING: dict[str, Any] = {
    "pending_action_id": None,
    "pending_loan_id": None,
    "pending_amount_cents": None,
}


def _fresh_turn() -> dict[str, Any]:
    return {
        "refusal_code": None,
        "target_loan_id": None,
        "amount_cents": None,
        "wants_ledger": False,
        "tool_calls": [],
        "tool_payload": {},
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reply": "",
        "outcome": Outcome.REFUSED.value,
    }


def parse_classification(text: str) -> dict[str, Any] | None:
    """Read the model's JSON strictly. Anything unexpected is a failure, not a guess."""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    intent = parsed.get("intent")
    if intent not in ALLOWED_INTENTS:
        return None

    amount = parsed.get("amount_cents")
    if amount is not None and (not isinstance(amount, int) or isinstance(amount, bool)):
        return None

    wants_ledger = parsed.get("wants_ledger", False)
    if not isinstance(wants_ledger, bool):
        wants_ledger = False

    return {"intent": intent, "amount_cents": amount, "wants_ledger": wants_ledger}


async def sanitize_node(state: AgentState) -> dict[str, Any]:
    settings = current_context().settings
    return {
        "sanitized": sanitize_message(
            state.get("message", ""), max_chars=settings.max_message_chars
        )
    }


async def classify_node(state: AgentState) -> dict[str, Any]:
    context = current_context()
    text = state.get("sanitized", "")
    updates = _fresh_turn()

    if looks_like_injection(text):
        # Logged and counted, not acted on. The text stays data either way.
        logger.info(
            "message contains instruction like text",
            extra={"session": redact_session_id(state.get("session_id"))},
        )

    if not text:
        return {
            **updates,
            **_CLEARED_PENDING,
            "intent": Intent.REFUSED.value,
            "refusal_code": "empty_message",
        }

    if is_explicit_cancellation(text):
        return {**updates, **_CLEARED_PENDING, "intent": Intent.CANCELLATION.value}

    if is_explicit_confirmation(text):
        if not state.get("pending_action_id"):
            return {
                **updates,
                **_CLEARED_PENDING,
                "intent": Intent.REFUSED.value,
                "refusal_code": "nothing_to_confirm",
            }
        return {
            **updates,
            "intent": Intent.CONFIRMATION.value,
            "target_loan_id": state.get("pending_loan_id"),
            "amount_cents": state.get("pending_amount_cents"),
        }

    reason = out_of_scope_reason(text)
    if reason is not None:
        # Fixed refusal, no model call.
        return {
            **updates,
            **_CLEARED_PENDING,
            "intent": Intent.OUT_OF_SCOPE.value,
            "refusal_code": reason,
        }

    try:
        reply = await context.llm.complete(
            [
                Message(role=role, content=content)
                for role, content in build_classifier_messages(text)
            ]
        )
    except LlmUnavailable:
        return {
            **updates,
            **_CLEARED_PENDING,
            "intent": Intent.REFUSED.value,
            "refusal_code": "model_unavailable",
        }

    updates["llm_calls"] = 1
    updates["prompt_tokens"] = reply.prompt_tokens
    updates["completion_tokens"] = reply.completion_tokens

    parsed = parse_classification(reply.text)
    if parsed is None:
        return {
            **updates,
            **_CLEARED_PENDING,
            "intent": Intent.REFUSED.value,
            "refusal_code": "classification_failed",
        }

    if parsed["intent"] == Intent.OUT_OF_SCOPE.value:
        return {
            **updates,
            **_CLEARED_PENDING,
            "intent": Intent.OUT_OF_SCOPE.value,
            "refusal_code": "generic",
        }

    loan_id, loan_refusal = resolve_target_loan(text, list(state.get("bound_loan_ids") or []))
    if loan_refusal is not None:
        return {
            **updates,
            **_CLEARED_PENDING,
            "intent": Intent.REFUSED.value,
            "refusal_code": loan_refusal,
        }

    if parsed["intent"] == Intent.PAYMENT_REQUEST.value:
        amount_refusal: str | None
        if mentions_negative_amount(text):
            # Read from the message rather than from the extracted number, so
            # this holds whether extraction returned a negative number, a
            # positive one, or nothing at all. Checking it second would switch
            # it off in the case extraction is most likely to fail on: a model
            # that sensibly declines to read "-5 dollars" as an amount leaves
            # nothing for the amount check to reject, and the refusal would
            # then read as a missing amount rather than an invalid one.
            #
            # One narrowing comes with the order: a message carrying both a
            # negative token and an amount over the cap now reports
            # invalid_amount rather than amount_above_cap. Both refuse and
            # neither writes.
            amount_refusal = "invalid_amount"
        else:
            amount_refusal = check_amount(
                parsed["amount_cents"], max_payment_cents=context.settings.max_payment_cents
            )
        if amount_refusal is not None:
            return {
                **updates,
                **_CLEARED_PENDING,
                "intent": Intent.REFUSED.value,
                "refusal_code": amount_refusal,
                "amount_cents": parsed["amount_cents"],
                "target_loan_id": loan_id,
            }
        return {
            **updates,
            **_CLEARED_PENDING,
            "intent": Intent.PAYMENT_REQUEST.value,
            "amount_cents": parsed["amount_cents"],
            "target_loan_id": loan_id,
        }

    return {
        **updates,
        **_CLEARED_PENDING,
        "intent": Intent.READ_QUESTION.value,
        "target_loan_id": loan_id,
        "wants_ledger": bool(parsed["wants_ledger"]),
    }


def route_after_classify(state: AgentState) -> str:
    intent = state.get("intent")
    if intent in {
        Intent.READ_QUESTION.value,
        Intent.PAYMENT_REQUEST.value,
        Intent.CONFIRMATION.value,
    }:
        return "tool_call"
    return "respond"


async def tool_call_node(state: AgentState) -> dict[str, Any]:
    context = current_context()
    intent = state.get("intent")
    loan_id = state.get("target_loan_id")
    called: list[str] = []

    try:
        if intent == Intent.READ_QUESTION.value:
            summary = await context.tools.call("get_loan_summary", {"loan_id": loan_id})
            called.append("get_loan_summary")
            payload: dict[str, Any] = {
                "summary_status": summary.status,
                "summary": summary.payload,
            }
            if state.get("wants_ledger") and summary.status == "ok":
                entries = await context.tools.call(
                    "get_ledger_entries", {"loan_id": loan_id, "limit": LEDGER_LINE_LIMIT}
                )
                called.append("get_ledger_entries")
                payload["ledger_status"] = entries.status
                payload["ledger"] = entries.payload
            return {"tool_calls": called, "tool_payload": payload}

        if intent == Intent.PAYMENT_REQUEST.value:
            preview = await context.tools.call(
                "prepare_payment", {"loan_id": loan_id, "amount_cents": state.get("amount_cents")}
            )
            called.append("prepare_payment")
            updates: dict[str, Any] = {
                "tool_calls": called,
                "tool_payload": {"preview_status": preview.status, "preview": preview.payload},
            }
            if preview.status == "ok":
                updates["pending_action_id"] = preview.payload.get("pending_action_id")
                updates["pending_loan_id"] = preview.payload.get("loan_id")
                updates["pending_amount_cents"] = preview.payload.get("amount_cents")
                logger.info(
                    "payment prepared",
                    extra={
                        "session": redact_session_id(state.get("session_id")),
                        "loan": redact_loan_id(preview.payload.get("loan_id")),
                        "amount_cents": preview.payload.get("amount_cents"),
                    },
                )
            return updates

        outcome = await context.tools.call(
            "confirm_payment", {"pending_action_id": state.get("pending_action_id")}
        )
        called.append("confirm_payment")
        updates = {
            "tool_calls": called,
            "tool_payload": {"outcome_status": outcome.status, "outcome": outcome.payload},
        }
        # A settled outcome retires the pending action. An unknown or unavailable
        # one keeps it, so the same idempotency key is reused if the caller tries
        # again and the ledger still applies the payment once.
        if outcome.status in {"ok", "rejected"}:
            updates.update(_CLEARED_PENDING)
        return updates

    except ToolTransportError as exc:
        logger.warning(
            "tool transport failed",
            extra={
                "session": redact_session_id(state.get("session_id")),
                "intent": intent,
                "error": str(exc),
            },
        )
        return {
            "tool_calls": called,
            "tool_payload": {
                "transport_error": True,
                "was_writing": intent == Intent.CONFIRMATION.value,
            },
        }


def _respond_to_read(payload: dict[str, Any]) -> tuple[str, str]:
    status = payload.get("summary_status")
    summary = payload.get("summary", {})
    if status == "ok":
        text = replies.loan_summary_reply(summary)
        if payload.get("ledger_status") == "ok":
            text = f"{text} {replies.ledger_reply(payload.get('ledger', {}))}"
        return text, Outcome.ANSWERED.value
    if status == "rejected":
        return (
            replies.rejection_reply(summary.get("error_code"), summary.get("message")),
            Outcome.REFUSED.value,
        )
    if status == "unknown":
        return replies.unavailable_reply(was_writing=False), Outcome.UNKNOWN.value
    return replies.unavailable_reply(was_writing=False), Outcome.UNAVAILABLE.value


def _respond_to_payment_request(payload: dict[str, Any]) -> tuple[str, str]:
    status = payload.get("preview_status")
    preview = payload.get("preview", {})
    if status == "ok":
        if preview.get("review_status") == "needs_review":
            # Prepared, but a reviewer has to clear it, so the reply must not
            # ask for a confirmation that would be refused.
            return replies.payment_held_reply(preview), Outcome.HELD.value
        return replies.payment_preview_reply(preview), Outcome.PREPARED.value
    if status == "rejected":
        return (
            replies.rejection_reply(preview.get("error_code"), preview.get("message")),
            Outcome.REFUSED.value,
        )
    if status == "unknown":
        return replies.unavailable_reply(was_writing=False), Outcome.UNKNOWN.value
    return replies.unavailable_reply(was_writing=False), Outcome.UNAVAILABLE.value


def _respond_to_confirmation(payload: dict[str, Any]) -> tuple[str, str]:
    status = payload.get("outcome_status")
    outcome = payload.get("outcome", {})
    if status == "ok":
        return replies.payment_applied_reply(outcome), Outcome.APPLIED.value
    if status == "rejected":
        return (
            replies.rejection_reply(outcome.get("error_code"), outcome.get("message")),
            Outcome.REFUSED.value,
        )
    if status == "unknown":
        # A timeout on a write. The reply must not claim either result.
        return replies.unknown_outcome_reply(), Outcome.UNKNOWN.value
    return replies.unavailable_reply(was_writing=True), Outcome.UNAVAILABLE.value


async def respond_node(state: AgentState) -> dict[str, Any]:
    context = current_context()
    intent = state.get("intent")
    payload = dict(state.get("tool_payload") or {})
    refusal_code = state.get("refusal_code")

    if payload.get("transport_error"):
        return {"reply": replies.tool_failure_reply(), "outcome": Outcome.FAILED.value}

    if intent == Intent.CANCELLATION.value:
        return {"reply": replies.cancelled_reply(), "outcome": Outcome.CANCELLED.value}

    if intent == Intent.OUT_OF_SCOPE.value:
        text = replies.OUT_OF_SCOPE_REPLIES.get(
            refusal_code or "generic", replies.OUT_OF_SCOPE_REPLIES["generic"]
        )
        return {"reply": text, "outcome": Outcome.REFUSED.value}

    if intent == Intent.REFUSED.value:
        if refusal_code == "amount_above_cap":
            text = replies.amount_above_cap_reply(
                int(state.get("amount_cents") or 0), context.settings.max_payment_cents
            )
        elif refusal_code == "model_unavailable":
            text = replies.unavailable_reply(was_writing=False)
        elif refusal_code == "classification_failed":
            text = replies.tool_failure_reply()
        else:
            text = replies.REFUSAL_REPLIES.get(
                refusal_code or "", replies.OUT_OF_SCOPE_REPLIES["generic"]
            )
        return {"reply": text, "outcome": Outcome.REFUSED.value}

    if intent == Intent.READ_QUESTION.value:
        text, outcome = _respond_to_read(payload)
    elif intent == Intent.PAYMENT_REQUEST.value:
        text, outcome = _respond_to_payment_request(payload)
    elif intent == Intent.CONFIRMATION.value:
        text, outcome = _respond_to_confirmation(payload)
    else:  # pragma: no cover - every intent above is handled
        text, outcome = replies.tool_failure_reply(), Outcome.FAILED.value

    return {"reply": text, "outcome": outcome}


def build_graph() -> StateGraph[AgentState, Any]:
    graph: StateGraph[AgentState, Any] = StateGraph(AgentState)
    graph.add_node("sanitize", sanitize_node)
    graph.add_node("classify", classify_node)
    graph.add_node("tool_call", tool_call_node)
    graph.add_node("respond", respond_node)

    graph.add_edge(START, "sanitize")
    graph.add_edge("sanitize", "classify")
    graph.add_conditional_edges(
        "classify", route_after_classify, {"tool_call": "tool_call", "respond": "respond"}
    )
    graph.add_edge("tool_call", "respond")
    graph.add_edge("respond", END)
    return graph
