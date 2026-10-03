"""ORM smoke tests for the domain models (Event, Reminder, RecurringExpense)."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

import app.db as db
from app.config import Settings
from app.db import Base
from app.models import Event, EventRecurrence, ExpensePeriod, RecurringExpense, Reminder


@pytest.fixture
def schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create a schema on a temp SQLite DB using the app engine (FK pragma on)."""

    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.sqlite3').as_posix()}"
    monkeypatch.setattr(db, "get_settings", lambda: Settings(database_url=url))
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)

    async def _create_schema() -> None:
        engine = db.get_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(_create_schema())
    yield
    asyncio.run(db.dispose_engine())


def test_event_create_and_read(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            event = Event(
                name="День рождения Маши",
                next_date=date(2026, 11, 18),
                recurrence=EventRecurrence.YEARLY,
                action_text="Поздравить",
            )
            session.add(event)
            await session.commit()
            event_id = event.id

        async with db.get_session() as session:
            loaded = await session.get(Event, event_id)
            assert loaded is not None
            assert loaded.name == "День рождения Маши"
            assert loaded.next_date == date(2026, 11, 18)
            assert loaded.recurrence is EventRecurrence.YEARLY
            assert loaded.action_text == "Поздравить"
            assert loaded.is_active is True
            assert loaded.created_at is not None
            assert loaded.updated_at is not None

    asyncio.run(_run())


def test_reminder_relationship_and_cascade(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            event = Event(
                name="Встреча с врачом",
                next_date=date(2026, 10, 15),
                recurrence=EventRecurrence.NONE,
            )
            session.add(event)
            await session.flush()
            session.add_all(
                [
                    Reminder(event=event, remind_at=date(2026, 10, 14)),
                    Reminder(event=event, remind_at=date(2026, 10, 15)),
                ]
            )
            await session.commit()
            event_id = event.id

        async with db.get_session() as session:
            loaded = await session.get(
                Event, event_id, options=[selectinload(Event.reminders)]
            )
            assert loaded is not None
            assert [r.remind_at for r in loaded.reminders] == [
                date(2026, 10, 14),
                date(2026, 10, 15),
            ]
            assert loaded.reminders[0].event is loaded

        # Deleting the event cascades to its reminders at the DB level.
        async with db.get_session() as session:
            event = await session.get(Event, event_id)
            await session.delete(event)
            await session.commit()

        async with db.get_session() as session:
            remaining = (
                await session.execute(text("SELECT COUNT(*) FROM reminders"))
            ).scalar_one()
            assert remaining == 0

    asyncio.run(_run())


def test_foreign_key_enforcement(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            session.add(Reminder(event_id=999_999, remind_at=date(2026, 1, 1)))
            with pytest.raises(IntegrityError):
                await session.commit()

    asyncio.run(_run())


def test_expense_amount_is_exact(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            expense = RecurringExpense(
                name="Spotify",
                amount_minor=29_900,
                currency="RUB",
                period=ExpensePeriod.MONTHLY,
                payment_day=15,
            )
            session.add(expense)
            await session.commit()
            expense_id = expense.id

        async with db.get_session() as session:
            loaded = await session.get(RecurringExpense, expense_id)
            assert loaded is not None
            assert loaded.amount_minor == 29_900
            assert isinstance(loaded.amount_minor, int)
            assert loaded.currency == "RUB"
            assert loaded.period is ExpensePeriod.MONTHLY
            assert loaded.payment_day == 15

    asyncio.run(_run())


def test_expense_constraints(schema: None) -> None:
    async def _attempt(**overrides) -> None:
        kwargs = dict(
            name="X",
            amount_minor=1000,
            currency="EUR",
            period=ExpensePeriod.MONTHLY,
            payment_day=5,
        )
        kwargs.update(overrides)
        async with db.get_session() as session:
            session.add(RecurringExpense(**kwargs))
            await session.commit()

    async def _run() -> None:
        for bad in (
            {"amount_minor": 0},
            {"amount_minor": -5},
            {"payment_day": 0},
            {"payment_day": 32},
            {"currency": ""},
        ):
            with pytest.raises(IntegrityError):
                await _attempt(**bad)

    asyncio.run(_run())


def test_recurrence_invalid_value_rejected(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO events "
                        "(name, next_date, recurrence, is_active, created_at, updated_at) "
                        "VALUES ('x', '2026-01-01', 'weekly', 1, "
                        "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                    )
                )

    asyncio.run(_run())
