"""Tests for pure recurring-expense calendar logic."""

from datetime import date

from app.expense_dates import (
    calculate_next_payment_date,
    calculate_previous_payment_date,
    count_payments,
    nearest_payment_date,
    next_payment_on_or_after,
)
from app.models.expense import ExpensePeriod


def test_monthly_regular() -> None:
    assert calculate_next_payment_date(
        date(2026, 1, 15), ExpensePeriod.MONTHLY, 15
    ) == date(2026, 2, 15)


def test_monthly_day_31_to_28() -> None:
    assert calculate_next_payment_date(
        date(2026, 1, 31), ExpensePeriod.MONTHLY, 31
    ) == date(2026, 2, 28)


def test_monthly_day_31_to_29_leap() -> None:
    assert calculate_next_payment_date(
        date(2028, 1, 31), ExpensePeriod.MONTHLY, 31
    ) == date(2028, 2, 29)


def test_monthly_day_31_restores_after_short_month() -> None:
    d = date(2026, 1, 31)
    d = calculate_next_payment_date(d, ExpensePeriod.MONTHLY, 31)
    assert d == date(2026, 2, 28)
    d = calculate_next_payment_date(d, ExpensePeriod.MONTHLY, 31)
    assert d == date(2026, 3, 31)
    d = calculate_next_payment_date(d, ExpensePeriod.MONTHLY, 31)
    assert d == date(2026, 4, 30)
    d = calculate_next_payment_date(d, ExpensePeriod.MONTHLY, 31)
    assert d == date(2026, 5, 31)


def test_monthly_year_transition() -> None:
    assert calculate_next_payment_date(
        date(2026, 12, 31), ExpensePeriod.MONTHLY, 31
    ) == date(2027, 1, 31)


def test_quarterly_regular() -> None:
    d = date(2026, 1, 15)
    for expected in (
        date(2026, 4, 15),
        date(2026, 7, 15),
        date(2026, 10, 15),
        date(2027, 1, 15),
    ):
        d = calculate_next_payment_date(d, ExpensePeriod.QUARTERLY, 15)
        assert d == expected


def test_quarterly_day_31_end_of_month() -> None:
    d = date(2026, 1, 31)
    for expected in (
        date(2026, 4, 30),
        date(2026, 7, 31),
        date(2026, 10, 31),
        date(2027, 1, 31),
    ):
        d = calculate_next_payment_date(d, ExpensePeriod.QUARTERLY, 31)
        assert d == expected


def test_quarterly_year_transition() -> None:
    assert calculate_next_payment_date(
        date(2026, 10, 15), ExpensePeriod.QUARTERLY, 15
    ) == date(2027, 1, 15)


def test_yearly_regular() -> None:
    assert calculate_next_payment_date(
        date(2026, 11, 18), ExpensePeriod.YEARLY, 18
    ) == date(2027, 11, 18)


def test_yearly_leap_day_feb29() -> None:
    # month comes from next_payment_date (Feb); day is payment_day 29.
    d = date(2026, 2, 28)
    d = calculate_next_payment_date(d, ExpensePeriod.YEARLY, 29)
    assert d == date(2027, 2, 28)
    d = calculate_next_payment_date(d, ExpensePeriod.YEARLY, 29)
    assert d == date(2028, 2, 29)


def test_yearly_year_transition() -> None:
    assert calculate_next_payment_date(
        date(2026, 12, 31), ExpensePeriod.YEARLY, 31
    ) == date(2027, 12, 31)


def test_previous_round_trips_next() -> None:
    d = date(2026, 2, 28)
    prev = calculate_previous_payment_date(d, ExpensePeriod.MONTHLY, 31)
    assert prev == date(2026, 1, 31)
    assert calculate_next_payment_date(prev, ExpensePeriod.MONTHLY, 31) == d


def test_next_on_or_after_monthly_skips_many() -> None:
    assert next_payment_on_or_after(
        date(2026, 1, 15), ExpensePeriod.MONTHLY, 15, date(2026, 9, 27)
    ) == date(2026, 10, 15)


def test_next_on_or_after_quarterly_skips_many() -> None:
    assert next_payment_on_or_after(
        date(2026, 1, 15), ExpensePeriod.QUARTERLY, 15, date(2026, 9, 27)
    ) == date(2026, 10, 15)


def test_next_on_or_after_yearly_skips_many() -> None:
    assert next_payment_on_or_after(
        date(2026, 11, 18), ExpensePeriod.YEARLY, 18, date(2029, 1, 1)
    ) == date(2029, 11, 18)


def test_next_on_or_after_already_future() -> None:
    assert next_payment_on_or_after(
        date(2026, 10, 15), ExpensePeriod.MONTHLY, 15, date(2026, 9, 27)
    ) == date(2026, 10, 15)


def test_count_payments_monthly_three() -> None:
    assert count_payments(
        date(2026, 1, 15), ExpensePeriod.MONTHLY, 15,
        date(2026, 1, 1), date(2026, 3, 31),
    ) == 3


def test_count_payments_inclusive_boundaries() -> None:
    # payment exactly on start and end are both counted
    assert count_payments(
        date(2026, 1, 15), ExpensePeriod.MONTHLY, 15,
        date(2026, 1, 15), date(2026, 3, 15),
    ) == 3


def test_count_payments_empty_range() -> None:
    assert count_payments(
        date(2026, 1, 15), ExpensePeriod.MONTHLY, 15,
        date(2026, 3, 1), date(2026, 1, 1),
    ) == 0


def test_count_payments_anchor_inside_range() -> None:
    # anchor itself is in range
    assert count_payments(
        date(2026, 2, 15), ExpensePeriod.MONTHLY, 15,
        date(2026, 2, 1), date(2026, 2, 28),
    ) == 1


def test_nearest_payment_date_reapplies_day() -> None:
    # day change 15 -> 20, reference 2026-10-15, after 2026-09-27 -> 2026-10-20
    assert nearest_payment_date(
        date(2026, 10, 15), ExpensePeriod.MONTHLY, 20, date(2026, 9, 27)
    ) == date(2026, 10, 20)


def test_nearest_payment_date_advances_when_passed() -> None:
    # 20th of October is before `after` -> November 20
    assert nearest_payment_date(
        date(2026, 10, 15), ExpensePeriod.MONTHLY, 20, date(2026, 10, 25)
    ) == date(2026, 11, 20)


def test_nearest_payment_date_keeps_payment_day_31() -> None:
    # reference was clamped to Feb 28 but payment_day stays 31 -> next is May 31
    assert nearest_payment_date(
        date(2026, 2, 28), ExpensePeriod.QUARTERLY, 31, date(2026, 3, 1)
    ) == date(2026, 5, 31)
