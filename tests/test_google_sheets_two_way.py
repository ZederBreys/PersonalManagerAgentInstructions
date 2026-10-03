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


def trim(row: list) -> list:
    """A row without its trailing empty cells (the API drops them when reading)."""
    cells = list(row)
    while cells and not str(cells[-1]).strip():
        cells.pop()
    return cells


class SheetStore:
    """In-memory spreadsheet with the real API's semantics.

    Values (``update`` only touches the cells it is given; a leading apostrophe
    forces text and is consumed), tabs with stable ids that survive renaming,
    hidden role markers, and the notes/colours written with ``batch_update``.
    """

    def __init__(self, sheets: dict[str, list[list[object]]] | None = None) -> None:
        self.sheets = {name: [list(r) for r in rows] for name, rows in (sheets or {}).items()}
        self._ids = {name: number for number, name in enumerate(self.sheets, start=1)}
        self._next_id = len(self._ids) + 1
        self.roles: dict[int, str] = {}
        self.notes: dict[tuple[int, int], str] = {}  # (sheet id, row) -> note
        self.colours: dict[tuple[int, int], dict] = {}
        self.updates: list[str] = []  # titles of the sheets that were written to
        self.batch_requests: list[dict] = []
        self.requests = {"read": 0, "write": 0}
        self.fail_full_write = False
        self.fail_batch_update = False
        self.on_read = None  # optional hook(store, range_name) called on every get_values

    # --- tabs -------------------------------------------------------------------
    # The ``*_sync`` methods hold the logic (a test double of the Google service
    # calls them from worker threads); the async ones are the client interface.
    def sheets_sync(self):
        from app.google_sheets.client import SheetInfo

        self.requests["read"] += 1
        return [SheetInfo(self._ids[name], name) for name in self.sheets]

    def roles_sync(self) -> dict[int, str]:
        self.requests["read"] += 1
        return dict(self.roles)

    def tag_sync(self, sheet_id: int, role: str) -> None:
        self.requests["write"] += 1
        self.roles[sheet_id] = role

    def add_sheet_sync(self, title: str) -> None:
        assert title not in self.sheets, "Sheets rejects duplicate tab titles"
        self.requests["write"] += 1
        self.sheets[title] = []
        self._ids[title] = self._next_id
        self._next_id += 1

    async def get_sheets(self):
        return self.sheets_sync()

    async def get_sheet_titles(self) -> list[str]:
        return list(self.sheets)

    async def get_sheet_roles(self) -> dict[int, str]:
        return self.roles_sync()

    async def tag_sheet(self, sheet_id: int, role: str) -> None:
        self.tag_sync(sheet_id, role)

    async def add_sheet(self, title: str) -> None:
        self.add_sheet_sync(title)

    def rename(self, old: str, new: str) -> None:
        """The user renames a tab: the id (and the hidden marker) stay."""
        self.sheets = {(new if name == old else name): rows for name, rows in self.sheets.items()}
        self._ids = {(new if name == old else name): sid for name, sid in self._ids.items()}

    def sheet_id(self, title: str) -> int:
        return self._ids[title]

    # --- reading ------------------------------------------------------------------
    @staticmethod
    def _column(letter: str) -> int:
        return ord(letter) - ord("A")

    def _rows(self, range_name: str) -> list[list[object]]:
        name, _, cells = range_name.partition("!")
        if name not in self.sheets:
            raise GoogleSheetsError(message=f"Unable to parse range: {range_name}", http_status=400)
        rows = [list(r) for r in self.sheets[name]]
        match = re.fullmatch(r"([A-Z]):([A-Z])", cells)
        if match:  # whole columns, e.g. A:I
            last = self._column(match.group(2)) + 1
            rows = [r[:last] for r in rows]
        while rows and not any(str(c).strip() for c in rows[-1]):
            rows.pop()  # the API omits trailing empty rows
        return rows[:1] if cells == "1:1" else rows

    async def get_values(self, range_name: str) -> list[list[object]]:
        if self.on_read is not None:
            self.on_read(self, range_name)
        self.requests["read"] += 1
        return self._rows(range_name)

    async def batch_get(self, ranges) -> list[list[list[object]]]:
        self.requests["read"] += 1
        return [self._rows(r) for r in ranges]

    # --- writing --------------------------------------------------------------------
    def _set(self, name: str, row: int, col: int, value: object) -> None:
        rows = self.sheets.setdefault(name, [])
        while len(rows) <= row:
            rows.append([])
        while len(rows[row]) <= col:
            rows[row].append("")
        rows[row][col] = value

    async def update_values(self, range_name: str, values: list[list[object]]) -> None:
        self.update_sync(range_name, values)

    def update_sync(self, range_name: str, values: list[list[object]]) -> None:
        name, _, cells = range_name.partition("!")
        match = re.fullmatch(r"([A-Z])(\d+)", cells)
        assert match, cells
        if self.fail_full_write and cells == "A2" and len(values[0]) > 1:
            raise GoogleSheetsError(message="write failed")
        self.requests["write"] += 1
        self.updates.append(name)
        start_col, start_row = self._column(match.group(1)), int(match.group(2)) - 1
        for i, row in enumerate(values):
            for j, value in enumerate(row):
                if isinstance(value, str) and value.startswith("'"):
                    value = value[1:]  # USER_ENTERED: the apostrophe forces text and is consumed
                self._set(name, start_row + i, start_col + j, value)

    async def clear(self, range_name: str) -> None:
        self.clear_sync(range_name)

    def clear_sync(self, range_name: str) -> None:
        name, _, cells = range_name.partition("!")
        match = re.fullmatch(r"([A-Z])(\d+):([A-Z])(\d+)", cells)
        assert match, cells
        self.requests["write"] += 1
        first_col, last_col = self._column(match.group(1)), self._column(match.group(3))
        rows = self.sheets.get(name, [])
        for index in range(int(match.group(2)) - 1, min(int(match.group(4)), len(rows))):
            for col in range(first_col, min(last_col + 1, len(rows[index]))):
                rows[index][col] = ""  # only the cleared columns, like the real API

    async def batch_update(self, requests) -> None:
        self.batch_update_sync(requests)

    def batch_update_sync(self, requests) -> None:
        if self.fail_batch_update:
            raise GoogleSheetsError(message="batchUpdate failed")
        self.requests["write"] += 1
        self.batch_requests.extend(requests)
        for request in requests:
            update = request.get("updateCells")
            if not update:
                continue
            sheet_id, row = update["range"]["sheetId"], update["range"]["startRowIndex"] + 1
            cell = update["rows"][0]["values"][0]
            note = cell.get("note")
            colour = cell.get("userEnteredFormat", {}).get("backgroundColor")
            for store, value in ((self.notes, note), (self.colours, colour)):
                if value is None:
                    store.pop((sheet_id, row), None)
                else:
                    store[(sheet_id, row)] = value

    # --- test helpers ----------------------------------------------------------------
    def note(self, title: str, row: int) -> str | None:
        return self.notes.get((self._ids[title], row))

    def colour(self, title: str, row: int) -> dict | None:
        return self.colours.get((self._ids[title], row))

    def data_rows(self, name: str) -> list[list[object]]:
        rows = [r for r in self.sheets.get(name, [])[1:] if any(str(c).strip() for c in r)]
        return [trim([str(c) for c in r]) for r in rows]


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
    new_row=["", "Новый платеж", "9.99", "eur", "monthly", "5", "Подписки", "2026-11-05", "", ""],
    invalid_new_row=["", "Плохой платеж", "1,234.56", "eur", "", "5", "", "2026-11-05", "", ""],
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
    assert store.data_rows(kind.sheet) == [trim([str(c) for c in row])]


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
    assert rows[0] == trim([str(c) for c in kind.invalid_new_row])  # untouched
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
    assert rows[0] == ["", "Без даты"]
