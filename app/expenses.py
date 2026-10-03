"""Deterministic domain operations for RecurringExpense.

A ``RecurringExpense`` is a *planned* recurring cost, not an actual payment:
it describes when the next charge is expected, not that money was debited.

As in Stage 2, the caller owns the transaction: these functions ``flush()``
but never ``commit()``.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.expense_dates import (
    calculate_next_payment_date,
    count_payments,
    nearest_payment_date,
    next_payment_on_or_after,
)
from app.models.expense import ExpensePeriod, RecurringExpense


def _coerce_period(period: object) -> ExpensePeriod:
    if isinstance(period, ExpensePeriod):
        return period
    try:
        return ExpensePeriod(period)  # type: ignore[arg-type]
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Invalid period: {period!r}") from exc


def _clean_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Expense name must be a non-empty string")
    return name.strip()


def _clean_currency(currency: str) -> str:
    if not isinstance(currency, str) or not currency.strip():
        raise ValueError("Currency must be a non-empty string")
    return currency.strip().upper()


def _validate_amount(amount_minor: int) -> int:
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise ValueError("amount_minor must be an integer")
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")
    return amount_minor


def _validate_payment_day(payment_day: int) -> int:
    if not isinstance(payment_day, int) or isinstance(payment_day, bool):
        raise ValueError("payment_day must be an integer")
    if not 1 <= payment_day <= 31:
        raise ValueError("payment_day must be between 1 and 31")
    return payment_day


def _validate_reminder_days(days: int) -> int:
    if not isinstance(days, int) or isinstance(days, bool) or days < 0:
        raise ValueError(f"reminder_days_before must be a non-negative integer: {days!r}")
    return days


async def create_expense(
    session: AsyncSession,
    *,
    name: str,
    amount_minor: int,
    currency: str,
    period: ExpensePeriod | str,
    payment_day: int,
    next_payment_date: date,
    category: str | None = None,
    is_active: bool = True,
    reminder_days_before: int = 3,
) -> RecurringExpense:
    """Create a RecurringExpense and add it to the session (caller commits)."""

    expense = RecurringExpense(
        name=_clean_name(name),
        amount_minor=_validate_amount(amount_minor),
        currency=_clean_currency(currency),
        period=_coerce_period(period),
        payment_day=_validate_payment_day(payment_day),
        next_payment_date=next_payment_date,
        category=category,
        is_active=is_active,
        reminder_days_before=_validate_reminder_days(reminder_days_before),
    )
    session.add(expense)
    await session.flush()
    return expense


async def get_expense(session: AsyncSession, expense_id: int) -> RecurringExpense | None:
    """Return a RecurringExpense by id, or ``None`` if it does not exist."""

    return await session.get(RecurringExpense, expense_id)


async def list_expenses(
    session: AsyncSession, *, active_only: bool = True
) -> list[RecurringExpense]:
    """Return expenses ordered by next_payment_date; by default only active ones."""

    stmt = select(RecurringExpense).order_by(
        RecurringExpense.next_payment_date, RecurringExpense.id
    )
    if active_only:
        stmt = stmt.where(RecurringExpense.is_active.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_expense(
    session: AsyncSession,
    expense: RecurringExpense,
    *,
    name: str | None = None,
    amount_minor: int | None = None,
    currency: str | None = None,
    period: ExpensePeriod | str | None = None,
    payment_day: int | None = None,
    category: str | None = None,
    is_active: bool | None = None,
    next_payment_date: date | None = None,
    reminder_days_before: int | None = None,
    today: date | None = None,
) -> RecurringExpense:
    """Update fields of ``expense`` (caller commits).

    ``payment_day`` and ``period`` are schedule settings, so when either
    actually changes and ``next_payment_date`` is *not* explicitly provided,
    the next payment date is recomputed as the nearest schedule date ``>= today``
    under the new settings. An explicit ``next_payment_date`` always wins over
    this automatic recalculation.

    Changing name / amount_minor / currency / category / is_active alone never
    touches ``next_payment_date``.
    """

    reference = expense.next_payment_date
    schedule_changed = False

    if name is not None:
        expense.name = _clean_name(name)
    if amount_minor is not None:
        expense.amount_minor = _validate_amount(amount_minor)
    if currency is not None:
        expense.currency = _clean_currency(currency)
    if period is not None:
        new_period = _coerce_period(period)
        if new_period is not expense.period:
            expense.period = new_period
            schedule_changed = True
    if payment_day is not None:
        new_day = _validate_payment_day(payment_day)
        if new_day != expense.payment_day:
            expense.payment_day = new_day
            schedule_changed = True
    if category is not None:
        expense.category = category
    if is_active is not None:
        expense.is_active = is_active
    if reminder_days_before is not None:
        expense.reminder_days_before = _validate_reminder_days(reminder_days_before)

    if next_payment_date is not None:
        expense.next_payment_date = next_payment_date
    elif schedule_changed:
        if reference is None:
            raise ValueError("Cannot recompute next_payment_date without an existing value")
        today = today or date.today()
        expense.next_payment_date = nearest_payment_date(
            reference, expense.period, expense.payment_day, today
        )

    await session.flush()
    return expense


async def delete_expense(session: AsyncSession, expense: RecurringExpense) -> None:
    """Delete a RecurringExpense (caller commits)."""

    await session.delete(expense)
    await session.flush()


async def advance_expense(
    session: AsyncSession, expense: RecurringExpense
) -> RecurringExpense:
    """Advance ``expense`` by exactly one period."""

    if expense.next_payment_date is None:
        raise ValueError("Cannot advance an expense without next_payment_date")
    expense.next_payment_date = calculate_next_payment_date(
        expense.next_payment_date, expense.period, expense.payment_day
    )
    await session.flush()
    return expense


async def advance_due_expense(
    session: AsyncSession, expense: RecurringExpense, *, today: date | None = None
) -> RecurringExpense:
    """Advance ``expense`` past any missed periods to the first date >= ``today``.

    Idempotent: once ``next_payment_date`` is no longer before ``today``, the
    expense is left untouched.
    """

    today = today or date.today()
    if expense.next_payment_date is not None and expense.next_payment_date < today:
        expense.next_payment_date = next_payment_on_or_after(
            expense.next_payment_date, expense.period, expense.payment_day, today
        )
        await session.flush()
    return expense


async def advance_due_expenses(
    session: AsyncSession, *, today: date | None = None
) -> list[RecurringExpense]:
    """Advance all active, overdue expenses and return the changed ones."""

    today = today or date.today()
    advanced: list[RecurringExpense] = []
    for expense in await list_expenses(session, active_only=True):
        if expense.next_payment_date is not None and expense.next_payment_date < today:
            expense.next_payment_date = next_payment_on_or_after(
                expense.next_payment_date, expense.period, expense.payment_day, today
            )
            advanced.append(expense)
    if advanced:
        await session.flush()
    return advanced


def calculate_expected_cost(
    expense: RecurringExpense, start_date: date, end_date: date
) -> int:
    """Expected integer cost of ``expense`` for payments in ``[start_date, end_date]``.

    Counts scheduled payment dates inside the inclusive range and multiplies by
    ``amount_minor``, staying entirely in integer arithmetic.
    """

    if expense.next_payment_date is None:
        return 0
    number = count_payments(
        expense.next_payment_date,
        expense.period,
        expense.payment_day,
        start_date,
        end_date,
    )
    return number * expense.amount_minor
