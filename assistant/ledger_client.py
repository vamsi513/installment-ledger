"""An HTTP client for the ledger API.

The assistant is an ordinary client of the ledger. It gets no special access, no
database connection and no import of the service layer. Everything it knows
comes back through these four calls.

Failures are separated into three kinds, because the agent has to say something
different about each one: the request timed out and the outcome is unknown, the
ledger is unavailable, or the ledger refused the request and said why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .redaction import redact_loan_id

logger = logging.getLogger("assistant.ledger")


class LedgerError(RuntimeError):
    """Base class for everything that can go wrong talking to the ledger."""


class LedgerTimeout(LedgerError):
    """The request timed out. The write may or may not have landed."""


class LedgerUnavailable(LedgerError):
    """The ledger could not be reached, or answered 5xx."""


@dataclass
class LedgerRejected(LedgerError):
    """The ledger answered 4xx with its standard error envelope."""

    status_code: int
    code: str
    message: str
    details: dict[str, Any]

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _parse_error(response: httpx.Response) -> LedgerRejected:
    try:
        envelope = response.json().get("error", {})
    except ValueError:
        envelope = {}
    return LedgerRejected(
        status_code=response.status_code,
        code=str(envelope.get("code") or "unknown_error"),
        message=str(envelope.get("message") or response.reason_phrase or "the ledger refused"),
        details=dict(envelope.get("details") or {}),
    )


class LedgerClient:
    """Thin async wrapper over the ledger's public endpoints."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
                response = await client.request(method, path, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            raise LedgerTimeout(f"{method} {path} timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LedgerUnavailable(f"{method} {path} failed: {type(exc).__name__}") from exc

        if response.status_code >= 500:
            raise LedgerUnavailable(f"{method} {path} returned {response.status_code}")
        if response.status_code >= 400:
            raise _parse_error(response)

        try:
            body = response.json()
        except ValueError as exc:
            raise LedgerUnavailable(f"{method} {path} returned a body that is not JSON") from exc
        return response.status_code, body

    async def health(self) -> dict[str, Any]:
        _, body = await self._request("GET", "/health")
        return body

    async def get_loan(self, loan_id: str) -> dict[str, Any]:
        _, body = await self._request("GET", f"/loans/{loan_id}")
        return body

    async def get_ledger(self, loan_id: str) -> dict[str, Any]:
        _, body = await self._request("GET", f"/loans/{loan_id}/ledger")
        return body

    async def create_payment(
        self, loan_id: str, amount_cents: int, *, idempotency_key: str
    ) -> dict[str, Any]:
        """Post a payment. Safe to retry with the same key; the ledger dedupes."""
        logger.info(
            "posting payment",
            extra={"loan": redact_loan_id(loan_id), "amount_cents": amount_cents},
        )
        _, body = await self._request(
            "POST",
            f"/loans/{loan_id}/payments",
            json_body={"amount_cents": amount_cents},
            headers={"Idempotency-Key": idempotency_key},
        )
        return body
