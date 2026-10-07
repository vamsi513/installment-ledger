"""Request validation.

Amounts arrive as integer cents and the APR arrives as integer basis points, so
no float ever enters the service. Unknown fields are rejected, which keeps the
idempotency request hash a faithful description of what was asked for.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from .schedule import Cadence

MIN_PRINCIPAL_CENTS = 100
MAX_PRINCIPAL_CENTS = 100_000_000
MIN_INSTALLMENTS = 2
MAX_INSTALLMENTS = 60
MAX_APR_BPS = 5_000


class CreateLoanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_cents: int = Field(ge=MIN_PRINCIPAL_CENTS, le=MAX_PRINCIPAL_CENTS)
    apr_bps: int = Field(ge=0, le=MAX_APR_BPS)
    installment_count: int = Field(ge=MIN_INSTALLMENTS, le=MAX_INSTALLMENTS)
    cadence: Cadence
    start_date: date


class CreatePaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_cents: int = Field(ge=1, le=MAX_PRINCIPAL_CENTS)
