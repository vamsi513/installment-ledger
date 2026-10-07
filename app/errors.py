"""Errors that map onto HTTP responses with a single body shape."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An error the client can act on, carrying a stable machine readable code."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class LoanNotFound(ApiError):
    status_code = 404
    code = "loan_not_found"


class LoanClosed(ApiError):
    status_code = 422
    code = "loan_closed"


class Overpayment(ApiError):
    status_code = 422
    code = "overpayment"


class MissingIdempotencyKey(ApiError):
    status_code = 400
    code = "missing_idempotency_key"


class IdempotencyKeyConflict(ApiError):
    status_code = 409
    code = "idempotency_key_conflict"


class IdempotencyRequestInFlight(ApiError):
    status_code = 409
    code = "idempotency_request_in_flight"
