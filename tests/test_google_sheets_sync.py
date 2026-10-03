"""Tests for Google Sheets export/import orchestration (fake client, real DB)."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app import db
from app import events, expenses, reminders
from app.google_sheets import (
    SheetValidationError,
    export_events,
    export_expenses,
    export_reminders,
    import_events,
    import_expenses,
)
from app.google_sheets.client import GoogleSheetsError
from app.google_sheets.mappers import EVENT_HEADERS, EXPENSE_HEADERS, REMINDER_HEADERS


class FakeClient:
    def __init__(
        self,
        sheets: dict[str, list[list[object]]] | None = None,
        *,
        fail_update: bool = False,
        fail_clear: bool = False,
    ) -> None:
        self.sheets = sheets or {}
        self.cleared: list[str] = []
        self.updates: dict[str, list[list[object]]] = {}
        self.fail_update = fail_update
        self.fail_clear = fail_clear

    async def get_values(self, range_name: str) -> list[list[object]]:
        name = range_name.split("!")[0]
        return self.sheets.get(name, [])

    async def update_values(self, range_name: str, values: list[list[object]]) -> None:
        if self.fail_update:
            raise GoogleSheetsError(message="write failed")
        self.updates[range_name] = values

    async def clear(self, range_name: str) -> None:
        if self.fail_clear:
            raise GoogleSheetsError(message="clear failed")
        self.cleared.append(range_name)


# --- export -----------------------------------------------------------------

def test_export_events_writes_headers_and_rows(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            e1 = await events.create_event(session, name="A", next_date=date(2026, 11, 1))
            e2 = await events.create_event(
                session, name="B", next_date=date(2026, 10, 1), is_active=False
            )
            await session.commit()
            client = FakeClient()
            await export_events(session, client)

        assert client.cleared == []
        assert client.updates["Events"] == [
            EVENT_HEADERS,
            [e2.id, "B", "2026-10-01", "none", "0", "", "нет"],
            [e1.id, "A", "2026-11-01", "none", "0", "", "да"],
        ]

    asyncio.run(_run())


def test_export_events_empty_dataset(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            client = FakeClient()
            await export_events(session, client)
        assert client.updates["Events"] == [EVENT_HEADERS]

    asyncio.run(_run())


def test_export_expenses_writes_rows(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            exp = await expenses.create_expense(
                session,
                name="Netflix",
                amount_minor=1299,
                currency="USD",
                period="monthly",
                payment_day=1,
                next_payment_date=date(2026, 10, 1),
            )
            await session.commit()
            client = FakeClient()
            await export_expenses(session, client)

        assert client.cleared == []
        assert client.updates["Expenses"] == [
            EXPENSE_HEADERS,
            [exp.id, "Netflix", "12.99", "USD", "monthly", 1, "", "2026-10-01", "да"],
        ]

    asyncio.run(_run())


def test_export_reminders_writes_rows(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            e = await events.create_event(session, name="E", next_date=date(2026, 10, 15))
            await reminders.generate_reminders(session, e, offsets=(0,))
            await session.commit()
            rems = await reminders.list_reminders(session)
            client = FakeClient()
            await export_reminders(session, client)

        assert client.updates["Reminders"] == [
            REMINDER_HEADERS,
            [rems[0].id, e.id, "2026-10-15", "нет", "нет", ""],
        ]

    asyncio.run(_run())


# --- import: events ---------------------------------------------------------

def test_import_events_updates_existing_event(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            e = await events.create_event(session, name="Old", next_date=date(2026, 10, 15))
            await session.commit()
            eid = e.id

        async with db.get_session() as session:
            client = FakeClient(
                {"Events": [EVENT_HEADERS, [eid, "New", "2026-11-01", "", "", "", ""]]}
            )
            errors = await import_events(session, client)
            await session.commit()

        async with db.get_session() as session:
            reloaded = await events.get_event(session, eid)

        assert errors == []
        assert reloaded.name == "New"
        assert reloaded.next_date == date(2026, 11, 1)

    asyncio.run(_run())


def test_import_events_updates_reminder_offsets_via_domain(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            e = await events.create_event(session, name="E", next_date=date(2026, 10, 15))
            await session.commit()
            eid = e.id

        async with db.get_session() as session:
            client = FakeClient({"Events": [EVENT_HEADERS, [eid, "", "", "", "7", "", ""]]})
            errors = await import_events(session, client)
            await session.commit()

        async with db.get_session() as session:
            reloaded = await events.get_event(session, eid)
            rems = await reminders.get_reminders_for_event(session, eid)

        assert errors == []
        assert reloaded.reminder_offsets == [7]
        assert [r.remind_at for r in rems] == [date(2026, 10, 8)]

    asyncio.run(_run())


def test_import_events_unknown_id_rejected(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            client = FakeClient({"Events": [EVENT_HEADERS, [999, "X", "", "", "", "", ""]]})
            errors = await import_events(session, client)

        assert len(errors) == 1
        assert isinstance(errors[0], SheetValidationError)
        assert errors[0].row_number == 2
        assert "Unknown event ID: 999" in str(errors[0])

    asyncio.run(_run())


def test_import_events_empty_id_rejected(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            client = FakeClient({"Events": [EVENT_HEADERS, ["", "X", "", "", "", "", ""]]})
            errors = await import_events(session, client)

        assert len(errors) == 1
        assert "ID is empty" in str(errors[0])

    asyncio.run(_run())


def test_import_events_invalid_row_leaves_db_unchanged(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            e = await events.create_event(session, name="Keep", next_date=date(2026, 10, 15))
            await session.commit()
            eid = e.id

        async with db.get_session() as session:
            client = FakeClient(
                {
                    "Events": [
                        EVENT_HEADERS,
                        [eid, "Changed", "2026-11-01", "", "", "", ""],
                        [eid + 1, "X", "not-a-date", "", "", "", ""],
                    ]
                }
            )
            errors = await import_events(session, client)

        async with db.get_session() as session:
            reloaded = await events.get_event(session, eid)

        assert len(errors) == 1
        assert reloaded.name == "Keep"

    asyncio.run(_run())


def test_import_events_multiple_invalid_rows(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            client = FakeClient(
                {
                    "Events": [
                        EVENT_HEADERS,
                        [1, "X", "bad-date", "", "", "", ""],
                        [2, "Y", "2026-10-15", "weekly", "", "", ""],
                    ]
                }
            )
            errors = await import_events(session, client)

        assert len(errors) == 2

    asyncio.run(_run())


# --- import: expenses -------------------------------------------------------

def test_import_expenses_updates_existing_expense(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            exp = await expenses.create_expense(
                session,
                name="Old",
                amount_minor=1000,
                currency="USD",
                period="monthly",
                payment_day=15,
                next_payment_date=date(2026, 10, 15),
            )
            await session.commit()
            eid = exp.id

        async with db.get_session() as session:
            client = FakeClient(
                {"Expenses": [EXPENSE_HEADERS, [eid, "New", "12.50", "eur", "", "", "Софт", "", ""]]}
            )
            errors = await import_expenses(session, client)
            await session.commit()

        async with db.get_session() as session:
            reloaded = await expenses.get_expense(session, eid)

        assert errors == []
        assert reloaded.name == "New"
        assert reloaded.amount_minor == 1250
        assert reloaded.currency == "EUR"
        assert reloaded.category == "Софт"

    asyncio.run(_run())


def test_import_expenses_unknown_id_rejected(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            client = FakeClient({"Expenses": [EXPENSE_HEADERS, [999, "X", "1.00", "", "", "", "", "", ""]]})
            errors = await import_expenses(session, client)

        assert len(errors) == 1
        assert "Unknown expense ID: 999" in str(errors[0])

    asyncio.run(_run())


def test_import_expenses_invalid_money_leaves_db_unchanged(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            exp = await expenses.create_expense(
                session,
                name="Keep",
                amount_minor=1000,
                currency="USD",
                period="monthly",
                payment_day=15,
                next_payment_date=date(2026, 10, 15),
            )
            await session.commit()
            eid = exp.id

        async with db.get_session() as session:
            client = FakeClient({"Expenses": [EXPENSE_HEADERS, [eid, "Changed", "1,234.56", "", "", "", "", "", ""]]})
            errors = await import_expenses(session, client)

        async with db.get_session() as session:
            reloaded = await expenses.get_expense(session, eid)

        assert len(errors) == 1
        assert reloaded.name == "Keep"
        assert reloaded.amount_minor == 1000

    asyncio.run(_run())


# --- export: safe overwrite (R1) --------------------------------------------

def test_export_events_clears_stale_tail_when_old_has_more_rows(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            e = await events.create_event(session, name="A", next_date=date(2026, 10, 1))
            await session.commit()

        async with db.get_session() as session:
            old = [
                EVENT_HEADERS,
                ["1", "Old1", "2026-01-01", "none", "0", "", "да"],
                ["2", "Old2", "2026-01-02", "none", "0", "", "да"],
            ]
            client = FakeClient({"Events": old})
            await export_events(session, client)

        assert client.updates["Events"] == [
            EVENT_HEADERS,
            [e.id, "A", "2026-10-01", "none", "0", "", "да"],
        ]
        assert client.cleared == ["Events!A3:G3"]

    asyncio.run(_run())


def test_export_write_failure_does_not_clear_old_data(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            e = await events.create_event(session, name="A", next_date=date(2026, 10, 1))
            await session.commit()
            old = [EVENT_HEADERS, ["1", "Old", "2026-01-01", "none", "0", "", "да"]]
            client = FakeClient({"Events": old}, fail_update=True)
            with pytest.raises(GoogleSheetsError):
                await export_events(session, client)

        assert client.cleared == []

    asyncio.run(_run())


def test_export_clear_failure_keeps_newly_written_data(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            e = await events.create_event(session, name="A", next_date=date(2026, 10, 1))
            await session.commit()
            old = [
                EVENT_HEADERS,
                ["1", "Old1", "2026-01-01", "none", "0", "", "да"],
                ["2", "Old2", "2026-01-02", "none", "0", "", "да"],
            ]
            client = FakeClient({"Events": old}, fail_clear=True)
            with pytest.raises(GoogleSheetsError):
                await export_events(session, client)

        assert client.updates["Events"] == [
            EVENT_HEADERS,
            [e.id, "A", "2026-10-01", "none", "0", "", "да"],
        ]

    asyncio.run(_run())


# --- import: transaction ownership / atomicity (D1) --------------------------

def test_import_expenses_execution_error_rolls_back_own_changes(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            x1 = await expenses.create_expense(
                session, name="X1", amount_minor=100, currency="USD", period="monthly",
                payment_day=1, next_payment_date=date(2026, 10, 1),
            )
            x2 = await expenses.create_expense(
                session, name="X2", amount_minor=200, currency="USD", period="monthly",
                payment_day=2, next_payment_date=date(2026, 10, 2),
            )
            x3 = await expenses.create_expense(
                session, name="X3", amount_minor=300, currency="USD", period="monthly",
                payment_day=3, next_payment_date=date(2026, 10, 3),
            )
            await session.commit()
            id1, id2, id3 = x1.id, x2.id, x3.id

        async with db.get_session() as session:
            x2 = await expenses.get_expense(session, id2)
            x2.next_payment_date = None
            await session.commit()

        async with db.get_session() as session:
            x3 = await expenses.get_expense(session, id3)
            x3.name = "X3-CALLER"

            client = FakeClient(
                {
                    "Expenses": [
                        EXPENSE_HEADERS,
                        [id1, "X1-IMPORTED", "", "", "", "", "", "", ""],
                        [id2, "", "", "", "quarterly", "", "", "", ""],
                    ]
                }
            )
            with pytest.raises(ValueError):
                await import_expenses(session, client)

            await session.commit()

        async with db.get_session() as session:
            x1 = await expenses.get_expense(session, id1)
            x2 = await expenses.get_expense(session, id2)
            x3 = await expenses.get_expense(session, id3)
            assert x1.name == "X1"
            assert x2.period.value == "monthly"
            assert x3.name == "X3-CALLER"

    asyncio.run(_run())


def test_import_events_execution_error_rolls_back_own_changes(schema: None, monkeypatch) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            e1 = await events.create_event(session, name="E1", next_date=date(2026, 10, 1))
            e2 = await events.create_event(session, name="E2", next_date=date(2026, 10, 2))
            e3 = await events.create_event(session, name="E3", next_date=date(2026, 10, 3))
            await session.commit()
            id1, id2, id3 = e1.id, e2.id, e3.id

        real_update = events.update_event
        counter = {"n": 0}

        async def flaky_update(session, event, **kwargs):
            counter["n"] += 1
            if counter["n"] >= 2:
                raise ValueError("boom")
            return await real_update(session, event, **kwargs)

        monkeypatch.setattr("app.events.update_event", flaky_update)

        async with db.get_session() as session:
            e3 = await events.get_event(session, id3)
            e3.name = "E3-CALLER"

            client = FakeClient(
                {
                    "Events": [
                        EVENT_HEADERS,
                        [id1, "E1-NEW", "", "", "", "", ""],
                        [id2, "E2-NEW", "", "", "", "", ""],
                    ]
                }
            )
            with pytest.raises(ValueError):
                await import_events(session, client)

            await session.commit()

        async with db.get_session() as session:
            e1 = await events.get_event(session, id1)
            e2 = await events.get_event(session, id2)
            e3 = await events.get_event(session, id3)
            assert e1.name == "E1"
            assert e2.name == "E2"
            assert e3.name == "E3-CALLER"

    asyncio.run(_run())


def test_import_does_not_commit_until_caller_commits(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            e = await events.create_event(session, name="Old", next_date=date(2026, 10, 15))
            await session.commit()
            eid = e.id

        async with db.get_session() as session:
            client = FakeClient({"Events": [EVENT_HEADERS, [eid, "New", "", "", "", "", ""]]})
            errors = await import_events(session, client)
            assert errors == []

        async with db.get_session() as session:
            reloaded = await events.get_event(session, eid)
            assert reloaded.name == "Old"

    asyncio.run(_run())


def test_export_then_import_roundtrip(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            exp = await expenses.create_expense(
                session, name="Netflix", amount_minor=1299, currency="USD",
                period="monthly", payment_day=1, next_payment_date=date(2026, 10, 1),
            )
            await session.commit()
            eid = exp.id

        async with db.get_session() as session:
            client = FakeClient()
            await export_expenses(session, client)
            rows = client.updates["Expenses"]

        async with db.get_session() as session:
            client = FakeClient({"Expenses": rows})
            errors = await import_expenses(session, client)
            await session.commit()

        async with db.get_session() as session:
            reloaded = await expenses.get_expense(session, eid)

        assert errors == []
        assert reloaded.name == "Netflix"
        assert reloaded.amount_minor == 1299
        assert reloaded.currency == "USD"
        assert reloaded.next_payment_date == date(2026, 10, 1)

    asyncio.run(_run())
