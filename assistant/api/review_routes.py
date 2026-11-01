"""Reviewer endpoints.

There is no authentication in this service. These endpoints are gated by a
shared token in a header so they are not simply open, and the identity written
to the audit trail is whatever ``ASSISTANT_REVIEWER_ID`` says. A shared token
says somebody knew the token; it does not say who they are. The audit trail is
therefore a record of decisions attributed to a configured name, not proof of
who made them.

With ``ASSISTANT_REVIEW_TOKEN`` unset, every one of these refuses, which is the
same default the streaming socket takes.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..config import Settings, get_settings
from ..redaction import redact_loan_id
from ..review import ReviewOutcome, ReviewService
from .routes import error_response

logger = logging.getLogger("assistant.review.api")

router = APIRouter(tags=["assistant-review"])

ReviewToken = Annotated[str | None, Header(alias="X-Review-Token")]

MAX_REASON_CHARS = 500
HELD_PAGE_LIMIT = 100

#: Outcome to HTTP status. A decision that changed nothing is not an error, so
#: an "already approved" answers 200 and says so.
_REFUSAL_STATUS: dict[ReviewOutcome, int] = {
    ReviewOutcome.NOT_FOUND: 404,
    ReviewOutcome.NOT_HELD: 409,
    ReviewOutcome.EXPIRED: 409,
    ReviewOutcome.REVIEW_DISABLED: 409,
}


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)


class RejectRequest(BaseModel):
    """A rejection has to say why. An approval may, but does not have to."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)


def _guard(token: str | None, settings: Settings) -> JSONResponse | None:
    """Refuse unless the shared token is configured and matches."""
    if not settings.review_api_token:
        return error_response(
            403,
            "review_token_not_configured",
            "ASSISTANT_REVIEW_TOKEN is not set, so the reviewer endpoints are closed.",
        )
    if token != settings.review_api_token:
        logger.warning("review request refused", extra={"reason": "bad_token"})
        return error_response(403, "bad_review_token", "The X-Review-Token header is wrong.")
    if not settings.review_enabled:
        return error_response(
            409,
            "review_not_enabled",
            "ASSISTANT_REVIEW_ENABLED is off, so no payment is ever held for review.",
        )
    return None


def _action_body(action: Any) -> dict[str, Any]:
    return {
        "pending_action_id": action.pending_action_id,
        "loan_id": action.loan_id,
        "amount_cents": action.amount_cents,
        "status": action.status.value,
        "created_at": action.created_at,
        "expires_at": action.expires_at,
        "expired": action.is_expired(),
    }


def _decision_response(result: Any, settings: Settings) -> Response:
    if result.is_refusal:
        return error_response(
            _REFUSAL_STATUS.get(result.outcome, 409), result.outcome.value, result.message
        )
    body: dict[str, Any] = {
        "outcome": result.outcome.value,
        "changed": result.changed,
        "message": result.message,
        "reviewer_id": settings.reviewer_id,
    }
    if result.action is not None:
        body["action"] = _action_body(result.action)
    return JSONResponse(status_code=200, content=body)


@router.get("/review/held")
def list_held(token: ReviewToken = None) -> Response:
    """Every prepared payment waiting for a decision, oldest first."""
    settings = get_settings()
    refusal = _guard(token, settings)
    if refusal is not None:
        return refusal

    service = ReviewService(settings)
    held = service.held(limit=HELD_PAGE_LIMIT)
    return JSONResponse(
        status_code=200,
        content={
            "count": len(held),
            "threshold_cents": settings.review_threshold_cents,
            "max_prepares_in_window": settings.review_max_prepares,
            "window_seconds": settings.review_window_seconds,
            "held": [_action_body(action) for action in held],
        },
    )


@router.post("/review/{pending_action_id}/approve")
def approve(
    pending_action_id: str, request: ReviewDecisionRequest, token: ReviewToken = None
) -> Response:
    settings = get_settings()
    refusal = _guard(token, settings)
    if refusal is not None:
        return refusal

    try:
        reviewer = settings.require_reviewer_id()
    except RuntimeError as exc:
        return error_response(409, "reviewer_id_not_configured", str(exc))

    service = ReviewService(settings)
    result = service.approve(pending_action_id, actor=reviewer, reason=request.reason)
    if result.action is not None:
        logger.info(
            "review decision",
            extra={
                "outcome": result.outcome.value,
                "loan": redact_loan_id(result.action.loan_id),
                "reviewer": reviewer,
            },
        )
    return _decision_response(result, settings)


@router.post("/review/{pending_action_id}/reject")
def reject(pending_action_id: str, request: RejectRequest, token: ReviewToken = None) -> Response:
    settings = get_settings()
    refusal = _guard(token, settings)
    if refusal is not None:
        return refusal

    try:
        reviewer = settings.require_reviewer_id()
    except RuntimeError as exc:
        return error_response(409, "reviewer_id_not_configured", str(exc))

    service = ReviewService(settings)
    result = service.reject(pending_action_id, actor=reviewer, reason=request.reason)
    if result.action is not None:
        logger.info(
            "review decision",
            extra={
                "outcome": result.outcome.value,
                "loan": redact_loan_id(result.action.loan_id),
                "reviewer": reviewer,
            },
        )
    return _decision_response(result, settings)


@router.get("/review/{pending_action_id}/audit")
def audit(pending_action_id: str, token: ReviewToken = None) -> Response:
    """The audit trail for one prepared payment, oldest entry first."""
    settings = get_settings()
    refusal = _guard(token, settings)
    if refusal is not None:
        return refusal

    service = ReviewService(settings)
    entries = service.entries_for(pending_action_id)
    return JSONResponse(
        status_code=200,
        content={
            "pending_action_id": pending_action_id,
            "count": len(entries),
            "entries": [entry.as_dict() for entry in entries],
        },
    )
