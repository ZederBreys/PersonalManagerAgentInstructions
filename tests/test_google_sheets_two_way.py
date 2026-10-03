"""Two-way Google Sheets sync for Events and Expenses (create/update, no delete).

Runs the production ``sheets_sync`` job against a stateful in-memory sheet that
mimics the real API: ``values.update`` overwrites only the cells it is given.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy import func, select

from app import db, events, expenses
from app.config import Settings
from app.google_sheets.client import GoogleSheetsError
from app.google_sheets.mappers import EVENT_HEADERS, EXPENSE_HEADERS
from app.google_sheets.sync import import_events, import_expenses
from app.jobs import Services, SheetsSyncError, sheets_sync
from app.models.event import Event
from app.models.expense import RecurringExpense

_PENDING = re.compile(r"^new-[0-9a-f]{12}$")


class SheetStore:
    """In-memory spreadsheet with real-API write semantics."""

    def __init__(self, sheets: dict[str, list[list[object]]] | None = None) -> None:
        self.sheets = {name: [list(r) for r in rows] for name, rows in (sheets or {}).items()}
        self.fail_full_write = False
        self.on_read = None  # optional hook(store, range_name) called on every read

    async def get_sheet_titles(self) -> list[str]:
        return list(self.sheets)

    async def add_sheet(self, title: str) -> None:
        self.sheets.setdefault(title, [])

    async def get_values(self, range_name: str) -> list[list[object]]:
        if self.on_read is not None:
            self.on_read(self, range_name)
        name, _, cells = range_name.partition("!")
        rows = [list(r) for r in self.sheets.get(name, [])]
        while rows and not any(str(c).strip() for c in rows[-1]):
            rows.pop()  # the API omits trailing empty rows
        return rows[:1] if cells == "1:1" else rows

    def _set(self, name: str, row: int, col: int, value: object) -> None:
        rows = self.sheets.setdefault(name, [])
        while len(rows) <= row:
            rows.append([])
        while len(rows[row]) <= col:
            rows[row].append("")
        rows[row][col] = value

    async def update_values(self, range_name: str, values: list[list[object]]) -> None:
        name, _, cells = range_name.partition("!")
        start = 0
        if cells:
            match = re.fullmatch(r"A(\d+)", cells)
            assert match, cells
            start = int(match.group(1)) - 1
        elif self.fail_full_write:
            raise GoogleSheetsError(message="write failed")
        for i, row in enumerate(values):
            for j, value in enumerate(row):
                self._set(name, start + i, j, value)

    async def clear(self, range_name: str) -> None:
        name, _, cells = range_name.partition("!")
        first, last = (int(n) for n in re.findall(r"\d+", cells))
        rows = self.sheets.get(name, [])
        for index in range(first - 1, min(last, len(rows))):
            rows[index] = []

    def data_rows(self, name: str) -> list[list[object]]:
        rows = [r for r in self.sheets.get(name, [])[1:] if any(str(c).strip() for c in r)]
        return [[str(c) for c in r] for r in rows]


@dataclass(frozen=True)
class Kind:
    sheet: str
    headers: list[str]
    model: type
    new_row: list[object]
    invalid_new_row: list[object]
    name_col: int
    bad_col: int
    bad_value: str
    importer: object

    async def create(self, name: str):
        async with db.get_session() as session:
            if self.model is Event:
                record = await events.create_event(
                    session, name=name, next_date=date(2026, 12, 1)
                )
            else:
                record = await expenses.create_expense(
                    session,
                    name=name,
                    amount_minor=500,
                    currency="EUR",
                    period="monthly",
                    payment_day=1,
                    next_payment_date=date(2026, 11, 1),
                )
            await session.commit()
            return record.id


EVENTS = Kind(
    sheet="Events",
    headers=EVENT_HEADERS,
    model=Event,
    new_row=["", "Новое событие", "2026-12-24", "yearly", "7", "Купить подарок", ""],
    invalid_new_row=["", "Плохое событие", "not-a-date", "", "", "", ""],
    name_col=1,
    bad_col=2,
    bad_value="31.02.2026",
    importer=import_events,
)
EXPENSES = Kind(
    sheet="Expenses",
    headers=EXPENSE_HEADERS,
    model=RecurringExpense,
    new_row=["", "Новый платеж", "9.99", "eur", "monthly", "5", "Подписки", "2026-11-05", ""],
    invalid_new_row=["", "Плохой платеж", "1,234.56", "eur", "", "5", "", "2026-11-05", ""],
    name_col=1,
    bad_col=2,
    bad_value="12.345",
    importer=import_expenses,
)
KINDS = pytest.mark.parametrize("kind", [EVENTS, EXPENSES], ids=["event", "expense"])


def _services(store: SheetStore) -> Services:
    return Services(settings=Settings(_env_file=None), sheets=store)  # type: ignore[arg-type]


def _sync(store: SheetStore) -> None:
    asyncio.run(sheets_sync(_services(store)))


def _sync_expecting_errors(store: SheetStore) -> None:
    with pytest.raises(SheetsSyncError):
        _sync(store)


async def _count(model: type) -> int:
    async with db.get_session() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def _all(model: type) -> list:
    async with db.get_session() as session:
        return list((await session.execute(select(model).order_by(model.id))).scalars())


def _records(kind: Kind) -> list:
    return asyncio.run(_all(kind.model))


def _store(kind: Kind, *rows: list[object]) -> SheetStore:
    return SheetStore({kind.sheet: [list(kind.headers), *[list(r) for r in rows]]})


# --- create ------------------------------------------------------------------


@KINDS
def test_new_row_creates_record_and_gets_id(schema: None, kind: Kind) -> None:
    store = _store(kind, kind.new_row)
    _sync(store)

    [record] = _records(kind)
    assert record.name == kind.new_row[1]
    assert _PENDING.match(record.sheet_key)
    [row] = store.data_rows(kind.sheet)
    assert row[0] == str(record.id)  # the generated ID is written back into the row
    assert row[1] == kind.new_row[1]


def test_new_event_row_values_are_applied(schema: None) -> None:
    _sync(_store(EVENTS, EVENTS.new_row))
    [event] = _records(EVENTS)
    assert event.next_date == date(2026, 12, 24)
    assert event.recurrence.value == "yearly"
    assert event.reminder_offsets == [7]
    assert event.action_text == "Купить подарок"
    assert event.is_active is True


def test_new_expense_row_values_are_applied(schema: None) -> None:
    _sync(_store(EXPENSES, EXPENSES.new_row))
    [expense] = _records(EXPENSES)
    assert expense.amount_minor == 999
    assert expense.currency == "EUR"
    assert expense.payment_day == 5
    assert expense.category == "Подписки"
    assert expense.next_payment_date == date(2026, 11, 5)


def test_new_expense_period_defaults_to_monthly(schema: None) -> None:
    row = list(EXPENSES.new_row)
    row[4] = ""
    _sync(_store(EXPENSES, row))
    [expense] = _records(EXPENSES)
    assert expense.period.value == "monthly"


@KINDS
def test_repeated_sync_does_not_duplicate(schema: None, kind: Kind) -> None:
    store = _store(kind, kind.new_row)
    _sync(store)
    _sync(store)
    _sync(store)
    assert len(_records(kind)) == 1
    assert len(store.data_rows(kind.sheet)) == 1


@KINDS
def test_new_row_missing_required_field_is_not_created(schema: None, kind: Kind) -> None:
    row = list(kind.new_row)
    row[2] = ""  # date / amount is required for a new row
    store = _store(kind, row)
    _sync_expecting_errors(store)
    assert _records(kind) == []
    assert store.data_rows(kind.sheet) == [[str(c) for c in row]]


# --- update ------------------------------------------------------------------


@KINDS
def test_existing_id_updates_record(schema: None, kind: Kind) -> None:
    record_id = asyncio.run(kind.create("Старое имя"))
    store = SheetStore()
    _sync(store)  # export the record so the sheet has its row
    store.sheets[kind.sheet][1][kind.name_col] = "Новое имя"

    _sync(store)

    [record] = _records(kind)
    assert record.id == record_id  # updated in place, ID never replaced
    assert record.name == "Новое имя"
    assert store.data_rows(kind.sheet)[0][:2] == [str(record_id), "Новое имя"]


# --- invalid rows ------------------------------------------------------------


@KINDS
def test_invalid_new_row_is_not_created_and_kept_as_typed(schema: None, kind: Kind) -> None:
    store = _store(kind, kind.invalid_new_row, kind.new_row)

    _sync_expecting_errors(store)

    [created] = _records(kind)  # the valid row is still imported
    assert created.name == kind.new_row[1]
    rows = store.data_rows(kind.sheet)
    assert rows[0] == [str(c) for c in kind.invalid_new_row]  # untouched
    assert rows[1][0] == str(created.id)


@KINDS
def test_invalid_update_keeps_db_and_users_row(schema: None, kind: Kind) -> None:
    record_id = asyncio.run(kind.create("Как было"))
    store = SheetStore()
    _sync(store)
    row = store.sheets[kind.sheet][1]
    row[kind.name_col] = "Новое имя"
    row[kind.bad_col] = kind.bad_value
    typed = [str(c) for c in row]

    _sync_expecting_errors(store)

    [record] = _records(kind)
    assert record.id == record_id
    assert record.name == "Как было"  # the whole invalid row is rejected
    assert store.data_rows(kind.sheet) == [typed]  # not overwritten by export
    _sync_expecting_errors(store)  # stays reported until the user fixes it
    assert store.data_rows(kind.sheet) == [typed]


@KINDS
def test_unknown_and_duplicate_ids_are_rejected_and_kept(schema: None, kind: Kind) -> None:
    record_id = asyncio.run(kind.create("Один"))
    store = SheetStore()
    _sync(store)
    original = list(store.sheets[kind.sheet][1])
    duplicate = list(original)
    duplicate[kind.name_col] = "Копия"
    unknown = list(original)
    unknown[0] = "999"
    store.sheets[kind.sheet] += [duplicate, unknown]

    _sync_expecting_errors(store)

    assert [r.id for r in _records(kind)] == [record_id]
    rows = store.data_rows(kind.sheet)
    assert [r[0] for r in rows] == [str(record_id), str(record_id), "999"]
    assert rows[1][kind.name_col] == "Копия"


# --- no delete ---------------------------------------------------------------


@KINDS
def test_deleted_row_does_not_delete_record(schema: None, kind: Kind) -> None:
    record_id = asyncio.run(kind.create("Не удалять"))
    store = SheetStore()
    _sync(store)
    del store.sheets[kind.sheet][1]

    _sync(store)

    assert [r.id for r in _records(kind)] == [record_id]
    assert store.data_rows(kind.sheet)[0][0] == str(record_id)  # shown again


# --- recovery / idempotency ------------------------------------------------------


@KINDS
def test_crash_after_db_commit_before_export_does_not_duplicate(
    schema: None, kind: Kind
) -> None:
    store = _store(kind, kind.new_row)

    async def import_and_commit_then_crash() -> None:
        async with db.get_session() as session:
            assert await kind.importer(session, store) == []
            await session.commit()
        # process dies here: the export with the real ID never happens

    asyncio.run(import_and_commit_then_crash())
    assert _PENDING.match(store.data_rows(kind.sheet)[0][0])
    assert len(_records(kind)) == 1

    _sync(store)

    [record] = _records(kind)
    assert store.data_rows(kind.sheet)[0][0] == str(record.id)


@KINDS
def test_crash_before_db_commit_creates_exactly_once(schema: None, kind: Kind) -> None:
    store = _store(kind, kind.new_row)

    async def import_then_crash_without_commit() -> None:
        async with db.get_session() as session:
            await kind.importer(session, store)
        # session closed without commit: the record was never saved

    asyncio.run(import_then_crash_without_commit())
    assert _records(kind) == []
    assert _PENDING.match(store.data_rows(kind.sheet)[0][0])

    _sync(store)
    _sync(store)

    assert len(_records(kind)) == 1


@KINDS
def test_failed_export_then_next_sync_does_not_duplicate(schema: None, kind: Kind) -> None:
    store = _store(kind, kind.new_row)
    store.fail_full_write = True
    with pytest.raises(GoogleSheetsError):
        _sync(store)
    assert len(_records(kind)) == 1

    store.fail_full_write = False
    _sync(store)

    [record] = _records(kind)
    assert store.data_rows(kind.sheet)[0][0] == str(record.id)


def test_row_edited_during_sync_is_not_stamped(schema: None) -> None:
    store = _store(EVENTS, EVENTS.new_row)
    reads = {"n": 0}

    def user_edits_before_stamping(s: SheetStore, range_name: str) -> None:
        if range_name != "Events":
            return  # e.g. ensure_workbook's header check
        reads["n"] += 1
        if reads["n"] == 2:  # between the import read and the key stamping
            s.sheets["Events"][1][1] = "Исправлено пользователем"

    store.on_read = user_edits_before_stamping
    _sync(store)
    assert _records(EVENTS) == []  # nothing created from a stale read
    assert store.data_rows("Events")[0][:2] == ["", "Исправлено пользователем"]

    store.on_read = None
    _sync(store)
    [event] = _records(EVENTS)
    assert event.name == "Исправлено пользователем"


# --- export merge --------------------------------------------------------------


def test_export_keeps_user_row_order_and_appends_new_records(schema: None) -> None:
    first = asyncio.run(EVENTS.create("Первое"))
    second = asyncio.run(EVENTS.create("Второе"))
    store = _store(
        EVENTS,
        [str(second), "Второе", "2026-12-01", "none", "0", "", "да"],
        [str(first), "Первое", "2026-12-01", "none", "0", "", "да"],
    )
    third = asyncio.run(EVENTS.create("Третье"))

    _sync(store)

    assert [r[0] for r in store.data_rows("Events")] == [str(second), str(first), str(third)]


def test_short_kept_row_does_not_inherit_stale_cells(schema: None) -> None:
    store = _store(
        EVENTS,
        ["", "", "", "", "", "", ""],
        ["", "Без даты"],  # invalid new row, shorter than the header
        ["999", "Old", "2026-01-01", "none", "0", "stale", "да"],
    )
    _sync_expecting_errors(store)
    rows = store.data_rows("Events")
    assert rows[0] == ["", "Без даты", "", "", "", "", ""]
