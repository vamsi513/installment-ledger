"""Request validation.

Amounts arrive as integer cents and the APR arrives as integer basis points, so
no float ever enters the service. The integer fields are strict: a JSON string
or a JSON number with a decimal point is rejected rather than coerced, because
quietly reinterpreting an amount is the wrong thing to do with money. Unknown
fields are rejected too, which keeps the idempotency request hash a faithful
description of what was asked for.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .schedule import Cadence

MIN_PRINCIPAL_CENTS = 100
MAX_PRINCIPAL_CENTS = 100_000_000
MIN_INSTALLMENTS = 2
MAX_INSTALLMENTS = 60
MAX_APR_BPS = 5_000


class CreateLoanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_cents: StrictInt = Field(ge=MIN_PRINCIPAL_CENTS, le=MAX_PRINCIPAL_CENTS)
    apr_bps: StrictInt = Field(ge=0, le=MAX_APR_BPS)
    installment_count: StrictInt = Field(ge=MIN_INSTALLMENTS, le=MAX_INSTALLMENTS)
    cadence: Cadence
    start_date: date


class CreatePaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_cents: StrictInt = Field(ge=1, le=MAX_PRINCIPAL_CENTS)
