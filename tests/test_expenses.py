"""Domain operation tests for RecurringExpense."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

import app.db as db
from app.expenses import (
    advance_due_expense,
    advance_due_expenses,
    advance_expense,
    calculate_expected_cost,
    create_expense,
    delete_expense,
    get_expense,
    list_expenses,
    update_expense,
)
from app.models.expense import ExpensePeriod


def _run(coro) -> None:
    asyncio.run(coro)


def test_create_and_get(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s,
                name="  Netflix  ",
                amount_minor=1590,
                currency="eur",
                period=ExpensePeriod.MONTHLY,
                payment_day=15,
                next_payment_date=date(2026, 10, 15),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            got = await get_expense(s, eid)
            assert got is not None
            assert got.name == "Netflix"
            assert got.amount_minor == 1590
            assert got.currency == "EUR"
            assert got.period is ExpensePeriod.MONTHLY
            assert got.payment_day == 15
            assert got.next_payment_date == date(2026, 10, 15)
            assert got.is_active is True

    _run(_r())


def test_create_rejects_empty_name(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            for bad in ("", "   "):
                with pytest.raises(ValueError):
                    await create_expense(
                        s,
                        name=bad,
                        amount_minor=100,
                        currency="RUB",
                        period=ExpensePeriod.MONTHLY,
                        payment_day=1,
                        next_payment_date=date(2026, 1, 1),
                    )

    _run(_r())


def test_create_rejects_invalid_amount(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            for bad in (0, -5):
                with pytest.raises(ValueError):
                    await create_expense(
                        s,
                        name="x",
                        amount_minor=bad,
                        currency="RUB",
                        period=ExpensePeriod.MONTHLY,
                        payment_day=1,
                        next_payment_date=date(2026, 1, 1),
                    )

    _run(_r())


def test_create_rejects_invalid_period(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            with pytest.raises(ValueError):
                await create_expense(
                    s,
                    name="x",
                    amount_minor=100,
                    currency="RUB",
                    period="weekly",
                    payment_day=1,
                    next_payment_date=date(2026, 1, 1),
                )

    _run(_r())


def test_create_rejects_invalid_payment_day(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            for bad in (0, 32):
                with pytest.raises(ValueError):
                    await create_expense(
                        s,
                        name="x",
                        amount_minor=100,
                        currency="RUB",
                        period=ExpensePeriod.MONTHLY,
                        payment_day=bad,
                        next_payment_date=date(2026, 1, 1),
                    )

    _run(_r())


def test_create_rejects_empty_currency(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            with pytest.raises(ValueError):
                await create_expense(
                    s,
                    name="x",
                    amount_minor=100,
                    currency="  ",
                    period=ExpensePeriod.MONTHLY,
                    payment_day=1,
                    next_payment_date=date(2026, 1, 1),
                )

    _run(_r())


def test_list_active_sorted(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            await create_expense(
                s, name="B", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=1,
                next_payment_date=date(2026, 5, 1),
            )
            await create_expense(
                s, name="A", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=1,
                next_payment_date=date(2026, 3, 1),
            )
            await create_expense(
                s, name="C", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=1,
                next_payment_date=date(2026, 7, 1), is_active=False,
            )
            await s.commit()

            active = await list_expenses(s)
            assert [e.name for e in active] == ["A", "B"]
            all_expenses = await list_expenses(s, active_only=False)
            assert [e.name for e in all_expenses] == ["A", "B", "C"]

    _run(_r())


def test_update_unrelated_fields_do_not_change_date(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="Old", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 10, 15),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            await update_expense(
                s, exp,
                name="New", amount_minor=250, currency="usd",
                category="entertainment", is_active=False,
            )
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            assert exp.name == "New"
            assert exp.amount_minor == 250
            assert exp.currency == "USD"
            assert exp.period is ExpensePeriod.MONTHLY
            assert exp.payment_day == 15
            assert exp.category == "entertainment"
            assert exp.is_active is False
            # unrelated fields must never touch next_payment_date
            assert exp.next_payment_date == date(2026, 10, 15)

    _run(_r())


def test_delete_expense(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=1,
                next_payment_date=date(2026, 1, 1),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            await delete_expense(s, exp)
            await s.commit()
        async with db.get_session() as s:
            assert await get_expense(s, eid) is None

    _run(_r())


def test_advance_expense_one_period(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=31,
                next_payment_date=date(2026, 1, 31),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            await advance_expense(s, exp)
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            assert exp.next_payment_date == date(2026, 2, 28)

    _run(_r())


def test_advance_due_expense_skips_many(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 1, 15),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            await advance_due_expense(s, exp, today=date(2026, 9, 27))
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            assert exp.next_payment_date == date(2026, 10, 15)

    _run(_r())


def test_advance_due_expense_idempotent(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 1, 15),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            await advance_due_expense(s, exp, today=date(2026, 9, 27))
            await advance_due_expense(s, exp, today=date(2026, 9, 27))
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            assert exp.next_payment_date == date(2026, 10, 15)

    _run(_r())


def test_advance_due_expenses_bulk_excludes_inactive(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            await create_expense(
                s, name="monthly", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 1, 15),
            )
            await create_expense(
                s, name="yearly", amount_minor=100, currency="RUB",
                period=ExpensePeriod.YEARLY, payment_day=18,
                next_payment_date=date(2024, 11, 18),
            )
            await create_expense(
                s, name="inactive", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 1, 15), is_active=False,
            )
            await s.commit()
        async with db.get_session() as s:
            advanced = await advance_due_expenses(s, today=date(2026, 9, 27))
            await s.commit()
            assert sorted(e.name for e in advanced) == ["monthly", "yearly"]
        async with db.get_session() as s:
            by_name = {e.name: e for e in await list_expenses(s, active_only=False)}
            assert by_name["monthly"].next_payment_date == date(2026, 10, 15)
            assert by_name["yearly"].next_payment_date == date(2026, 11, 18)
            assert by_name["inactive"].next_payment_date == date(2026, 1, 15)

    _run(_r())


def test_update_payment_day_recomputes_next(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 10, 15),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            await update_expense(s, exp, payment_day=20, today=date(2026, 9, 27))
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            # nearest date >= today under the new day-of-month 20
            assert exp.next_payment_date == date(2026, 10, 20)

    _run(_r())


def test_update_payment_day_already_passed_recomputes(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 10, 15),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            await update_expense(s, exp, payment_day=20, today=date(2026, 10, 25))
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            # 20th of October is already past -> next month
            assert exp.next_payment_date == date(2026, 11, 20)

    _run(_r())


def test_update_period_recomputes_next(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 10, 15),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            await update_expense(
                s, exp, period=ExpensePeriod.QUARTERLY, today=date(2026, 10, 20)
            )
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            # October 15 has passed; quarterly next occurrence is January 15
            assert exp.next_payment_date == date(2027, 1, 15)

    _run(_r())


def test_update_next_payment_date_override(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 10, 15),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            await update_expense(s, exp, next_payment_date=date(2026, 12, 5))
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            assert exp.next_payment_date == date(2026, 12, 5)
            await advance_expense(s, exp)
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            # manual override anchors the schedule: +1 month, day snapped to 15
            assert exp.next_payment_date == date(2027, 1, 15)

    _run(_r())


def test_update_explicit_next_payment_date_wins_over_schedule_change(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 10, 15),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            await update_expense(
                s, exp,
                payment_day=20, next_payment_date=date(2026, 12, 5),
                today=date(2026, 9, 27),
            )
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            assert exp.payment_day == 20
            # explicit date is preserved; it is not auto-recomputed
            assert exp.next_payment_date == date(2026, 12, 5)

    _run(_r())


def test_update_payment_day_31_preserved(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=100, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=31,
                next_payment_date=date(2026, 1, 31),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            await advance_expense(s, exp)
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            assert exp.next_payment_date == date(2026, 2, 28)
            # change period only; payment_day must remain 31, not collapse to 28
            await update_expense(
                s, exp, period=ExpensePeriod.QUARTERLY, today=date(2026, 3, 1)
            )
            await s.commit()
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            assert exp.payment_day == 31
            # phase is February (2026-02-28); +3 months -> May 31
            assert exp.next_payment_date == date(2026, 5, 31)

    _run(_r())


def test_amount_minor_stays_int(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="big", amount_minor=1_000_000_000, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=1,
                next_payment_date=date(2026, 1, 1),
            )
            await s.commit()
            eid = exp.id
        async with db.get_session() as s:
            exp = await get_expense(s, eid)
            assert isinstance(exp.amount_minor, int)
            assert exp.amount_minor == 1_000_000_000

    _run(_r())


def test_calculate_expected_cost_one_payment(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=10000, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 1, 15),
            )
            await s.commit()
            assert calculate_expected_cost(
                exp, date(2026, 1, 1), date(2026, 1, 31)
            ) == 10000

    _run(_r())


def test_calculate_expected_cost_multiple(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=10000, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=1,
                next_payment_date=date(2026, 1, 1),
            )
            await s.commit()
            total = calculate_expected_cost(
                exp, date(2026, 1, 1), date(2026, 3, 31)
            )
            assert total == 30000
            assert isinstance(total, int)

    _run(_r())


def test_calculate_expected_cost_no_payments(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=10000, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=15,
                next_payment_date=date(2026, 1, 15),
            )
            await s.commit()
            assert calculate_expected_cost(
                exp, date(2026, 1, 16), date(2026, 2, 14)
            ) == 0

    _run(_r())


def test_calculate_expected_cost_large_amount(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            exp = await create_expense(
                s, name="x", amount_minor=1_000_000_000, currency="RUB",
                period=ExpensePeriod.MONTHLY, payment_day=1,
                next_payment_date=date(2026, 1, 1),
            )
            await s.commit()
            total = calculate_expected_cost(
                exp, date(2026, 1, 1), date(2026, 3, 31)
            )
            assert total == 3_000_000_000
            assert isinstance(total, int)

    _run(_r())
