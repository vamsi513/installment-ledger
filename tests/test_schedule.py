"""Schedule arithmetic.

The grid below checks every schedule invariant across 8 plan sizes, both
cadences, 5 APRs and 12 principal amounts, which is 960 distinct schedules.
"""

from __future__ import annotations

import itertools
from datetime import date
from decimal import Decimal

import pytest

from app.money import round_cents
from app.schedule import (
    BASIS_POINTS_PER_UNIT,
    DAYS_PER_YEAR,
    Cadence,
    add_months,
    build_schedule,
    distribute_cents,
    due_dates,
    simple_interest_cents,
)

PRINCIPALS = [
    100,
    101,
    199,
    999,
    1_000,
    1_001,
    4_999,
    10_000,
    33_333,
    123_457,
    999_999,
    7_777_777,
]
INSTALLMENT_COUNTS = [2, 3, 4, 5, 6, 12, 24, 60]
APR_BPS = [0, 1, 599, 1_999, 5_000]
START_DATES = [date(2026, 1, 15), date(2026, 1, 31), date(2025, 11, 30)]


@pytest.mark.parametrize(
    ("installment_count", "cadence", "apr_bps"),
    list(itertools.product(INSTALLMENT_COUNTS, list(Cadence), APR_BPS)),
)
def test_schedule_invariants_hold_across_the_grid(
    installment_count: int, cadence: Cadence, apr_bps: int
) -> None:
    for principal_cents in PRINCIPALS:
        schedule = build_schedule(
            principal_cents=principal_cents,
            apr_bps=apr_bps,
            installment_count=installment_count,
            cadence=cadence,
            start_date=date(2026, 1, 15),
        )
        context = f"principal={principal_cents} n={installment_count} {cadence} apr={apr_bps}"
        amounts = [item.amount_cents for item in schedule.installments]

        assert len(amounts) == installment_count, context
        assert sum(amounts) == schedule.total_cents, context
        assert schedule.total_cents == principal_cents + schedule.interest_cents, context
        assert all(amount > 0 for amount in amounts), context
        # Leftover cents land on the earliest installments, so amounts never
        # increase and never differ by more than one cent.
        assert amounts == sorted(amounts, reverse=True), context
        assert max(amounts) - min(amounts) <= 1, context
        assert [item.sequence for item in schedule.installments] == list(
            range(1, installment_count + 1)
        ), context
        dates = [item.due_date for item in schedule.installments]
        assert dates == sorted(dates), context
        assert all(due > date(2026, 1, 15) for due in dates), context


@pytest.mark.parametrize(
    ("total_cents", "installment_count", "expected"),
    [
        (1_000, 3, [334, 333, 333]),
        (1_000, 4, [250, 250, 250, 250]),
        (10, 4, [3, 3, 2, 2]),
        (100, 6, [17, 17, 17, 17, 16, 16]),
        (7, 2, [4, 3]),
        (120_000, 12, [10_000] * 12),
    ],
)
def test_distribute_cents_gives_the_remainder_to_the_earliest_installments(
    total_cents: int, installment_count: int, expected: list[int]
) -> None:
    assert distribute_cents(total_cents, installment_count) == expected
    assert sum(expected) == total_cents


@pytest.mark.parametrize("installment_count", INSTALLMENT_COUNTS)
@pytest.mark.parametrize("total_cents", [100, 101, 1_000, 99_999, 1_000_003])
def test_distribution_remainder_is_exactly_the_leading_installments(
    total_cents: int, installment_count: int
) -> None:
    amounts = distribute_cents(total_cents, installment_count)
    base, remainder = divmod(total_cents, installment_count)
    assert amounts[:remainder] == [base + 1] * remainder
    assert amounts[remainder:] == [base] * (installment_count - remainder)


@pytest.mark.parametrize("cadence", list(Cadence))
@pytest.mark.parametrize("installment_count", [2, 4, 12])
def test_zero_apr_adds_no_interest(cadence: Cadence, installment_count: int) -> None:
    schedule = build_schedule(
        principal_cents=123_457,
        apr_bps=0,
        installment_count=installment_count,
        cadence=cadence,
        start_date=date(2026, 3, 1),
    )
    assert schedule.interest_cents == 0
    assert schedule.total_cents == 123_457
    assert sum(item.amount_cents for item in schedule.installments) == 123_457


@pytest.mark.parametrize("apr_bps", [1, 599, 1_999, 5_000])
@pytest.mark.parametrize("cadence", list(Cadence))
def test_interest_matches_the_documented_formula(apr_bps: int, cadence: Cadence) -> None:
    principal_cents = 250_000
    schedule = build_schedule(
        principal_cents=principal_cents,
        apr_bps=apr_bps,
        installment_count=12,
        cadence=cadence,
        start_date=date(2026, 1, 15),
    )
    expected = round_cents(
        Decimal(principal_cents)
        * Decimal(apr_bps)
        / BASIS_POINTS_PER_UNIT
        * Decimal(schedule.term_days)
        / DAYS_PER_YEAR
    )
    assert schedule.interest_cents == expected
    assert schedule.interest_cents > 0


def test_interest_is_rounded_once_on_the_total_not_per_installment() -> None:
    # Rounding each installment separately would drift away from the single
    # rounded total; the schedule must still sum to that total exactly.
    schedule = build_schedule(
        principal_cents=99_999,
        apr_bps=1_237,
        installment_count=7,
        cadence=Cadence.MONTHLY,
        start_date=date(2026, 1, 31),
    )
    assert sum(item.amount_cents for item in schedule.installments) == schedule.total_cents
    assert schedule.total_cents == 99_999 + schedule.interest_cents


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.4", 0),
        ("0.5", 1),
        ("1.5", 2),
        ("2.5", 3),
        ("2.49999", 2),
        ("-0.5", -1),
        ("7", 7),
    ],
)
def test_rounding_is_half_away_from_zero(value: str, expected: int) -> None:
    assert round_cents(Decimal(value)) == expected


def test_term_days_runs_from_the_start_to_the_final_due_date() -> None:
    schedule = build_schedule(
        principal_cents=100_000,
        apr_bps=0,
        installment_count=4,
        cadence=Cadence.BIWEEKLY,
        start_date=date(2026, 1, 15),
    )
    assert schedule.term_days == 56
    assert schedule.installments[-1].due_date == date(2026, 3, 12)


def test_biweekly_due_dates_are_fourteen_days_apart() -> None:
    dates = due_dates(date(2026, 1, 15), 5, Cadence.BIWEEKLY)
    assert dates == [
        date(2026, 1, 29),
        date(2026, 2, 12),
        date(2026, 2, 26),
        date(2026, 3, 12),
        date(2026, 3, 26),
    ]


def test_monthly_due_dates_clamp_to_short_months_without_drifting() -> None:
    dates = due_dates(date(2026, 1, 31), 4, Cadence.MONTHLY)
    assert dates == [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31)]


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2024, 1, 31), 1, date(2024, 2, 29)),
        (date(2026, 12, 15), 1, date(2027, 1, 15)),
        (date(2026, 11, 30), 3, date(2027, 2, 28)),
        (date(2026, 5, 15), 24, date(2028, 5, 15)),
    ],
)
def test_add_months_clamps_the_day_and_rolls_the_year(
    start: date, months: int, expected: date
) -> None:
    assert add_months(start, months) == expected


@pytest.mark.parametrize("start_date", START_DATES)
def test_schedules_from_awkward_start_dates_still_sum_exactly(start_date: date) -> None:
    for installment_count in INSTALLMENT_COUNTS:
        schedule = build_schedule(
            principal_cents=1_234_567,
            apr_bps=1_499,
            installment_count=installment_count,
            cadence=Cadence.MONTHLY,
            start_date=start_date,
        )
        assert sum(item.amount_cents for item in schedule.installments) == schedule.total_cents


def test_simple_interest_is_linear_in_the_term() -> None:
    # No compounding: doubling the term doubles the interest.
    one_year = simple_interest_cents(1_000_000, 1_200, 365)
    two_years = simple_interest_cents(1_000_000, 1_200, 730)
    assert one_year == 120_000
    assert two_years == 240_000
