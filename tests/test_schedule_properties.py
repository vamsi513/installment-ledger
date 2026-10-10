"""Property based tests for the schedule arithmetic.

The example based tests in test_schedule.py pin specific known cases. These assert
the invariants hold across the whole input space Hypothesis can reach, which is how
an off by one cent in an unusual term length gets found.

Pure functions only, so no database and no fixtures.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from app.schedule import (
    Cadence,
    add_months,
    build_schedule,
    distribute_cents,
    due_dates,
    simple_interest_cents,
)

# Up to about ten million dollars, which is far past anything this service is for,
# and enough to catch an overflow or a rounding drift that only shows at scale.
amounts = st.integers(min_value=0, max_value=1_000_000_000)
positive_amounts = st.integers(min_value=1, max_value=1_000_000_000)
counts = st.integers(min_value=1, max_value=360)
aprs = st.integers(min_value=0, max_value=100_000)
start_dates = st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 12, 31))
cadences = st.sampled_from(list(Cadence))


class TestDistributeCents:
    @given(total=amounts, count=counts)
    def test_the_split_sums_to_the_total(self, total: int, count: int) -> None:
        """The invariant the whole service rests on: no cent is created or lost."""
        assert sum(distribute_cents(total, count)) == total

    @given(total=amounts, count=counts)
    def test_every_installment_is_within_one_cent_of_the_others(
        self, total: int, count: int
    ) -> None:
        parts = distribute_cents(total, count)
        assert max(parts) - min(parts) <= 1

    @given(total=amounts, count=counts)
    def test_the_remainder_goes_to_the_earliest_installments(self, total: int, count: int) -> None:
        """Documented behaviour: 1000 over 3 is 334, 333, 333, not 333, 333, 334."""
        parts = distribute_cents(total, count)
        assert parts == sorted(parts, reverse=True)

    @given(total=amounts, count=counts)
    def test_the_split_has_one_entry_per_installment(self, total: int, count: int) -> None:
        assert len(distribute_cents(total, count)) == count

    @given(total=amounts, count=counts)
    def test_no_installment_is_negative(self, total: int, count: int) -> None:
        assert all(part >= 0 for part in distribute_cents(total, count))


class TestInterest:
    @given(principal=amounts, apr=aprs, days=st.integers(min_value=0, max_value=36_500))
    def test_interest_is_never_negative(self, principal: int, apr: int, days: int) -> None:
        assert simple_interest_cents(principal, apr, days) >= 0

    @given(principal=amounts, days=st.integers(min_value=0, max_value=36_500))
    def test_zero_apr_is_exactly_zero_interest(self, principal: int, days: int) -> None:
        assert simple_interest_cents(principal, 0, days) == 0

    @given(
        principal=amounts,
        apr=aprs,
        days=st.integers(min_value=0, max_value=36_500),
        extra=st.integers(min_value=1, max_value=10_000),
    )
    def test_a_longer_term_never_costs_less(
        self, principal: int, apr: int, days: int, extra: int
    ) -> None:
        """Monotonic in term length. A rounding rule that broke this would mean a
        borrower could pay less by borrowing for longer."""
        assert simple_interest_cents(principal, apr, days) <= simple_interest_cents(
            principal, apr, days + extra
        )


class TestAddMonths:
    @given(start=start_dates, months=st.integers(min_value=0, max_value=600))
    def test_the_day_never_exceeds_the_target_month(self, start: date, months: int) -> None:
        """A loan starting on the 31st must not produce a 31st of February."""
        result = add_months(start, months)
        assert result.day <= start.day

    @given(start=start_dates, months=st.integers(min_value=1, max_value=600))
    def test_months_always_move_forward(self, start: date, months: int) -> None:
        assert add_months(start, months) > start

    @given(start=start_dates, months=st.integers(min_value=0, max_value=600))
    def test_no_drift_after_a_short_month(self, start: date, months: int) -> None:
        """Measured from the original date every time, so a 31st loan returns to the
        31st after passing through February rather than sticking at 28."""
        result = add_months(start, months)
        import calendar

        expected_day = min(start.day, calendar.monthrange(result.year, result.month)[1])
        assert result.day == expected_day


class TestDueDates:
    @given(start=start_dates, count=counts, cadence=cadences)
    def test_due_dates_are_strictly_increasing(
        self, start: date, count: int, cadence: Cadence
    ) -> None:
        dates = due_dates(start, count, cadence)
        assert dates == sorted(dates)
        assert len(set(dates)) == len(dates)

    @given(start=start_dates, count=counts, cadence=cadences)
    def test_the_first_due_date_is_after_the_start(
        self, start: date, count: int, cadence: Cadence
    ) -> None:
        assert due_dates(start, count, cadence)[0] > start


class TestBuildSchedule:
    @settings(max_examples=200)
    @given(
        principal=positive_amounts,
        apr=aprs,
        count=counts,
        cadence=cadences,
        start=start_dates,
    )
    def test_installments_sum_to_the_total_payable(
        self, principal: int, apr: int, count: int, cadence: Cadence, start: date
    ) -> None:
        """The headline invariant, asserted across the whole reachable input space."""
        schedule = build_schedule(
            principal_cents=principal,
            apr_bps=apr,
            installment_count=count,
            cadence=cadence,
            start_date=start,
        )
        assert sum(i.amount_cents for i in schedule.installments) == schedule.total_cents

    @given(
        principal=positive_amounts,
        apr=aprs,
        count=counts,
        cadence=cadences,
        start=start_dates,
    )
    def test_total_is_principal_plus_interest_exactly(
        self, principal: int, apr: int, count: int, cadence: Cadence, start: date
    ) -> None:
        schedule = build_schedule(
            principal_cents=principal,
            apr_bps=apr,
            installment_count=count,
            cadence=cadence,
            start_date=start,
        )
        assert schedule.total_cents == schedule.principal_cents + schedule.interest_cents

    @given(
        principal=positive_amounts,
        apr=aprs,
        count=counts,
        cadence=cadences,
        start=start_dates,
    )
    def test_sequences_are_one_based_and_contiguous(
        self, principal: int, apr: int, count: int, cadence: Cadence, start: date
    ) -> None:
        schedule = build_schedule(
            principal_cents=principal,
            apr_bps=apr,
            installment_count=count,
            cadence=cadence,
            start_date=start,
        )
        assert [i.sequence for i in schedule.installments] == list(range(1, count + 1))

    @given(
        principal=positive_amounts,
        apr=aprs,
        count=counts,
        cadence=cadences,
        start=start_dates,
    )
    def test_term_days_matches_the_last_due_date(
        self, principal: int, apr: int, count: int, cadence: Cadence, start: date
    ) -> None:
        schedule = build_schedule(
            principal_cents=principal,
            apr_bps=apr,
            installment_count=count,
            cadence=cadence,
            start_date=start,
        )
        assert schedule.term_days == (schedule.installments[-1].due_date - start).days
