"""Installment schedule math.

Pure functions with no database involvement, so the arithmetic can be tested on
its own. Two guarantees hold for every schedule this module produces:

* the installment amounts sum to exactly the total payable, and
* leftover cents are given to the earliest installments.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from .money import round_cents

BASIS_POINTS_PER_UNIT = Decimal(10_000)
DAYS_PER_YEAR = Decimal(365)
BIWEEKLY_PERIOD_DAYS = 14


class Cadence(StrEnum):
    """How far apart consecutive installments fall."""

    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"


@dataclass(frozen=True)
class ScheduledInstallment:
    sequence: int
    due_date: date
    amount_cents: int


@dataclass(frozen=True)
class Schedule:
    principal_cents: int
    interest_cents: int
    total_cents: int
    term_days: int
    installments: tuple[ScheduledInstallment, ...]


def add_months(start: date, months: int) -> date:
    """Advance a date by whole months, clamping the day to the target month.

    Always measured from the original date, so a loan starting on the 31st has
    due dates on the 31st of every month that has one, with no drift after a
    short month.
    """
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def due_dates(start_date: date, installment_count: int, cadence: Cadence) -> list[date]:
    """Return the due date of each installment, first one period after the start."""
    periods = range(1, installment_count + 1)
    if cadence is Cadence.BIWEEKLY:
        return [start_date + timedelta(days=BIWEEKLY_PERIOD_DAYS * n) for n in periods]
    return [add_months(start_date, n) for n in periods]


def simple_interest_cents(principal_cents: int, apr_bps: int, term_days: int) -> int:
    """Simple interest over the loan term, with no compounding.

    ``principal * apr * term_days / 365``, computed in Decimal and rounded to
    whole cents once by :func:`app.money.round_cents`.
    """
    if apr_bps == 0:
        return 0
    exact = (
        Decimal(principal_cents)
        * Decimal(apr_bps)
        / BASIS_POINTS_PER_UNIT
        * Decimal(term_days)
        / DAYS_PER_YEAR
    )
    return round_cents(exact)


def distribute_cents(total_cents: int, installment_count: int) -> list[int]:
    """Split a total into equal installments, earliest first for the remainder.

    With a total of 1000 cents over 3 installments the result is 334, 333, 333.
    """
    base, remainder = divmod(total_cents, installment_count)
    return [base + 1 if index < remainder else base for index in range(installment_count)]


def build_schedule(
    *,
    principal_cents: int,
    apr_bps: int,
    installment_count: int,
    cadence: Cadence,
    start_date: date,
) -> Schedule:
    """Build the full schedule for a loan."""
    dates = due_dates(start_date, installment_count, cadence)
    term_days = (dates[-1] - start_date).days
    interest_cents = simple_interest_cents(principal_cents, apr_bps, term_days)
    total_cents = principal_cents + interest_cents
    amounts = distribute_cents(total_cents, installment_count)
    installments = tuple(
        ScheduledInstallment(sequence=index + 1, due_date=due, amount_cents=amount)
        for index, (due, amount) in enumerate(zip(dates, amounts, strict=True))
    )
    return Schedule(
        principal_cents=principal_cents,
        interest_cents=interest_cents,
        total_cents=total_cents,
        term_days=term_days,
        installments=installments,
    )
