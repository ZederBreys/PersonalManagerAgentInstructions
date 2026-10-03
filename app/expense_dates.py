"""Pure, deterministic calendar logic for recurring expenses.

No SQLAlchemy dependency. These functions encode the recurrence rules for
monthly / quarterly / yearly expenses, including short-month clamping so that
a desired ``payment_day`` (e.g. 31) is never lost: it is remembered separately
from the concrete ``next_payment_date``.
"""

from __future__ import annotations

import calendar
from datetime import date

from app.models.expense import ExpensePeriod

# Number of months between consecutive payments for each period.
_PERIOD_MONTHS: dict[ExpensePeriod, int] = {
    ExpensePeriod.MONTHLY: 1,
    ExpensePeriod.QUARTERLY: 3,
    ExpensePeriod.YEARLY: 12,
}


def _clamp_day(year: int, month: int, day: int) -> date:
    """Build ``date(year, month, day)``, clamping day to the month's length."""

    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def _add_months(base: date, months: int, day: int) -> date:
    """Shift ``base`` by ``months`` and set the day to ``day`` (clamped)."""

    total = base.year * 12 + (base.month - 1) + months
    year = total // 12
    month = total % 12 + 1
    return _clamp_day(year, month, day)


def calculate_next_payment_date(
    current: date, period: ExpensePeriod, payment_day: int
) -> date:
    """Return the next payment date strictly after ``current``.

    The target month is ``current``'s month advanced by one period and the
    day is always ``payment_day`` (clamped to the target month's length).
    Because ``payment_day`` is used verbatim, a payment on day 31 correctly
    falls back to the last day of a short month and returns to 31 afterwards.
    """

    return _add_months(current, _PERIOD_MONTHS[period], payment_day)


def calculate_previous_payment_date(
    current: date, period: ExpensePeriod, payment_day: int
) -> date:
    """Return the payment date strictly before ``current``."""

    return _add_months(current, -_PERIOD_MONTHS[period], payment_day)


def nearest_payment_date(
    reference: date, period: ExpensePeriod, payment_day: int, after: date
) -> date:
    """Nearest schedule date >= ``after`` when the schedule settings change.

    ``reference`` is the existing ``next_payment_date``; its month/year is the
    current phase of the schedule. The day is re-applied as ``payment_day``
    (clamped) in that same month, then the schedule is advanced one period at a
    time until the result is no earlier than ``after``.

    This is used when ``payment_day`` or ``period`` changes: the desired day is
    never inherited from the clamped ``reference`` day (so 31 is not turned into
    28).
    """

    candidate = _clamp_day(reference.year, reference.month, payment_day)
    while candidate < after:
        candidate = calculate_next_payment_date(candidate, period, payment_day)
    return candidate


def next_payment_on_or_after(
    current: date, period: ExpensePeriod, payment_day: int, after: date
) -> date:
    """Advance ``current`` through any missed periods to the first date >= ``after``.

    Used to skip several missed payments in one go when the app was not running
    for a while (e.g. monthly 2026-01-15 with ``after`` 2026-09-27 -> 2026-10-15).
    """

    result = current
    while result < after:
        result = calculate_next_payment_date(result, period, payment_day)
    return result


def count_payments(
    anchor: date, period: ExpensePeriod, payment_day: int, start: date, end: date
) -> int:
    """Count payment dates inside the inclusive range ``[start, end]``.

    ``anchor`` is any concrete date on the schedule (typically the expense's
    ``next_payment_date``); the schedule is walked in both directions from it.
    """

    if start > end:
        return 0

    d = anchor
    while d > start:
        d = calculate_previous_payment_date(d, period, payment_day)
    while d < start:
        d = calculate_next_payment_date(d, period, payment_day)

    count = 0
    while d <= end:
        count += 1
        d = calculate_next_payment_date(d, period, payment_day)
    return count
