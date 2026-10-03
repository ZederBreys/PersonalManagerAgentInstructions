"""Pure, deterministic date/recurrence helpers (no SQLAlchemy dependency)."""

from __future__ import annotations

import calendar
from datetime import date

from app.models.event import EventRecurrence


def add_years(value: date, years: int) -> date:
    """Add ``years`` calendar years, clamping the day to the month's length.

    Handles February 29: ``2024-02-29 + 1 year == 2025-02-28``.
    """

    month = value.month
    day = value.day
    target_year = value.year + years
    last_day = calendar.monthrange(target_year, month)[1]
    return date(target_year, month, min(day, last_day))


def calculate_next_date(
    current_date: date, recurrence: EventRecurrence
) -> date | None:
    """Return the next occurrence one step after ``current_date``.

    A ``NONE`` event has no next occurrence (returns ``None``). A ``YEARLY``
    event returns ``current_date`` plus one calendar year (day clamped for the
    Feb 29 case).
    """

    if recurrence is EventRecurrence.NONE:
        return None
    if recurrence is EventRecurrence.YEARLY:
        return add_years(current_date, 1)
    raise ValueError(f"Unsupported recurrence: {recurrence!r}")


def yearly_occurrence(anchor: date, after: date) -> date:
    """Return the first yearly occurrence of ``anchor`` (month/day) >= ``after``.

    Always recomputes from the anchor so the original annual day (e.g. Feb 29)
    is preserved across non-leap years instead of drifting to Feb 28 forever.
    """

    def _in(year: int) -> date:
        last_day = calendar.monthrange(year, anchor.month)[1]
        return date(year, anchor.month, min(anchor.day, last_day))

    candidate = _in(after.year)
    if candidate < after:
        candidate = _in(after.year + 1)
    return candidate
