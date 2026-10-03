"""Tests for pure, deterministic date/recurrence calculations."""

from datetime import date

from app.dates import add_years, calculate_next_date, yearly_occurrence
from app.models import EventRecurrence


def test_calculate_next_date_none_is_stable() -> None:
    assert calculate_next_date(date(2026, 11, 18), EventRecurrence.NONE) is None


def test_calculate_next_date_yearly_regular() -> None:
    assert calculate_next_date(date(2026, 11, 18), EventRecurrence.YEARLY) == date(
        2027, 11, 18
    )


def test_calculate_next_date_yearly_leap_day() -> None:
    assert calculate_next_date(date(2024, 2, 29), EventRecurrence.YEARLY) == date(
        2025, 2, 28
    )


def test_calculate_next_date_into_leap_year_feb28() -> None:
    # Feb 28 is not a leap day; adding a year must stay Feb 28.
    assert calculate_next_date(date(2023, 2, 28), EventRecurrence.YEARLY) == date(
        2024, 2, 28
    )


def test_calculate_next_date_year_transition() -> None:
    assert calculate_next_date(date(2025, 12, 31), EventRecurrence.YEARLY) == date(
        2026, 12, 31
    )
    assert calculate_next_date(date(2024, 1, 1), EventRecurrence.YEARLY) == date(
        2025, 1, 1
    )


def test_add_years_leap_to_leap() -> None:
    assert add_years(date(2024, 2, 29), 4) == date(2028, 2, 29)


def test_yearly_occurrence_returns_to_leap_day() -> None:
    anchor = date(2024, 2, 29)
    assert yearly_occurrence(anchor, date(2024, 3, 1)) == date(2025, 2, 28)
    assert yearly_occurrence(anchor, date(2025, 3, 1)) == date(2026, 2, 28)
    assert yearly_occurrence(anchor, date(2026, 3, 1)) == date(2027, 2, 28)
    assert yearly_occurrence(anchor, date(2027, 3, 1)) == date(2028, 2, 29)


def test_yearly_occurrence_same_day_is_not_past() -> None:
    assert yearly_occurrence(date(2024, 11, 18), date(2026, 11, 18)) == date(
        2026, 11, 18
    )


def test_yearly_occurrence_overdue_advances_to_today_or_future() -> None:
    # overdue next_date 2024-11-18, today 2026-09-27 -> 2026-11-18
    assert yearly_occurrence(date(2024, 11, 18), date(2026, 9, 27)) == date(
        2026, 11, 18
    )
    # today is already past the anchor day this year -> next year
    assert yearly_occurrence(date(2024, 11, 18), date(2026, 11, 19)) == date(
        2027, 11, 18
    )
