"""Export (SQLite -> Sheets) and Import (Sheets -> SQLite) orchestration.

SQLite is the single source of truth; Google Sheets is the human interface for
entering and editing events and expenses.

Import (per row, through the existing domain functions):

* a row with a known ID updates that record;
* a valid row with an empty ID creates a record. Creation is two-phase so a
  crash can never produce a duplicate: the row's ID cell is first stamped with
  a one-time pending key (``new-…``), then the record is created with the same
  ``sheet_key``. A retried sync finds the record by that key;
* an invalid row is reported and skipped; the other rows are still applied;
* a record whose row is missing from the sheet is never deleted.

Export merges instead of overwriting: rows of known records are replaced by
their canonical form (which also turns a pending key into the real ID), while
rows SQLite does not know — invalid, unknown or not yet imported — and rows
whose import failed are written back untouched. Records without a row are
appended. The Reminders sheet is export-only and still fully overwritten.

The caller owns the top-level transaction: import applies each row inside its
own nested transaction (SAVEPOINT) and never commits, so a failing row rolls
back only its own changes and never commits unrelated pending state.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app import events, expenses, reminders, senders
from app.google_sheets.client import GoogleSheetsClient
from app.google_sheets.mappers import (
    EVENT_HEADERS,
    EXPENSE_HEADERS,
    INBOX_HEADERS,
    REMINDER_HEADERS,
    SENDER_HEADERS,
    event_to_row,
    expense_to_row,
    inbox_to_row,
    new_pending_key,
    parse_event_row,
    parse_expense_row,
    parse_row_key,
    parse_sender_row,
    reminder_to_row,
    require_new_event_fields,
    require_new_expense_fields,
    require_new_sender_fields,
    sender_to_row,
)
from app.models.allowed_sender import AllowedSender
from app.models.event import Event
from app.models.expense import ExpensePeriod, RecurringExpense
from app.models.inbox import InboxMessage

logger = logging.getLogger(__name__)

RowKey = int | str | None


class SheetValidationError(ValueError):
    """A problem with a specific sheet row (1-based sheet row number).

    ``key`` is the row's ID or pending key when known, so export can leave that
    row untouched until the user fixes it.
    """

    def __init__(self, row_number: int, message: str, *, key: RowKey = None) -> None:
        self.row_number = row_number
        self.message = message
        self.key = key
        super().__init__(f"Row {row_number}: {message}")


@dataclass(frozen=True)
class RowReport:
    """What import did with one sheet row (for the feedback written to the sheet)."""

    row_number: int  # 1-based sheet row
    record_id: int | None = None  # the record the row now stands for
    error: str | None = None  # why the row was not accepted
    deleted: bool = False  # the row asked for deletion and the record was deleted


def _is_empty_row(row: list[object], width: int | None = None) -> bool:
    """True when the table's own cells (the first ``width`` columns) are all empty.

    Cells further right belong to the user (notes); a row holding only a note is
    not a record, so it is neither imported nor flagged.
    """

    cells = row if width is None else row[:width]
    return all(cell is None or (isinstance(cell, str) and not cell.strip()) for cell in cells)


# --- reading the real values behind the displayed text ----------------------------------

_EXCEL_EPOCH = date(1899, 12, 30)  # serial 0 of Google Sheets / Excel
_NUMBER_NOISE = re.compile(r"(?i)\s|\u00a0|\u202f|₽|\$|€|руб\.?|р\.|rub|usd|eur")


def _serial_to_iso(serial: float) -> str | None:
    try:
        return (_EXCEL_EPOCH + timedelta(days=math.floor(serial))).isoformat()
    except (OverflowError, ValueError):
        return None


def _shown_number(shown: object) -> float | None:
    text = _NUMBER_NOISE.sub("", str(shown)).replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _normalize_rows(
    data: list[list[object]], raw: list[list[object]], columns: dict[int, str]
) -> tuple[list[list[object]], dict[int, str]]:
    """Replace displayed text by the underlying value in date/number columns.

    A date cell shown in any custom format (``суббота, 24 августа``) is read as
    its real date. A number cell whose displayed text disagrees with its value
    (for example a date format on an amount: it shows ``11.1`` but holds 12.5)
    is rejected with an explanation instead of being guessed. Returns the
    normalized rows and ``{sheet row number: message}`` for rejected rows.
    """

    normalized = [list(row) for row in data]
    errors: dict[int, str] = {}
    for i in range(1, min(len(data), len(raw))):
        for col, kind in columns.items():
            if col >= len(raw[i]) or col >= len(data[i]):
                continue
            value = raw[i][col]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue  # text: the displayed text is what was typed
            if kind == "date":
                iso = _serial_to_iso(value)
                if iso:
                    normalized[i][col] = iso
                continue
            shown = _shown_number(data[i][col])
            if shown is None or abs(shown - value) > 1e-9 * max(1.0, abs(value)):
                errors.setdefault(
                    i + 1,
                    f"The cell shows {data[i][col]!r} but holds the number {value:g}: its format "
                    "(e.g. a date format) hides the real value. Clear the cell's format "
                    "(Format -> Number -> Automatic) and retype it",
                )
            else:
                normalized[i][col] = repr(int(value)) if float(value).is_integer() else repr(float(value))
    return normalized, errors


def _safe_row_key(row: list[object]) -> RowKey:
    try:
        return parse_row_key(row)
    except ValueError:
        return None


@dataclass(frozen=True)
class _SheetSpec:
    """How one record type maps onto its sheet (shared import/export logic)."""

    sheet: str
    label: str
    headers: list[str]
    model: type
    parse: Callable[[list[object]], dict]
    require_new: Callable[[dict], None]
    to_row: Callable[[Any], list[object]]
    list_all: Callable[[AsyncSession], Awaitable[list]]
    create: Callable[[AsyncSession, dict], Awaitable[Any]]
    update: Callable[[AsyncSession, Any, dict], Awaitable[Any]]
    delete: Callable[[AsyncSession, Any], Awaitable[None]]
    # column index -> "date" | "number": read from the real value, not the display
    numeric_columns: dict[int, str]
    active_column: int  # the yes/no column (kept boolean where the user uses checkboxes)
    user_columns: int  # columns A.. that the user types into (the rest is bot-filled)


# --- Export -----------------------------------------------------------------

def _column_letter(index: int) -> str:
    """Return the spreadsheet column letter for a 0-based column index (A..Z)."""

    return chr(ord("A") + index)


async def _write_sheet(
    client: GoogleSheetsClient,
    sheet_name: str,
    width: int,
    rows: list[list[object] | None],
    *,
    old_length: int,
) -> None:
    """Write the data ``rows`` from A2 down and clear the stale tail.

    Row 1 (the header) is never written here: its text is the user's to rename.
    A row that is ``None`` is not ours (invalid, unknown, blank): all its cells
    are sent as JSON null, which the API skips, so the user's cells stay exactly
    as they are. Writing before clearing means a failed write never erases
    previous data. Rows are padded to the table width: ``values.update`` leaves
    cells it is not given untouched, so a short row moved onto a longer one
    would otherwise keep the old row's trailing cells.
    """

    if any(row is not None for row in rows):
        padded = [
            [None] * width if row is None else list(row) + [""] * (width - len(row)) for row in rows
        ]
        await client.update_values(f"{sheet_name}!A2", padded)
    last_row = 1 + len(rows)
    if old_length > last_row:
        last_col = _column_letter(width - 1)
        await client.clear(f"{sheet_name}!A{last_row + 1}:{last_col}{old_length}")


async def _overwrite_sheet(
    client: GoogleSheetsClient,
    sheet_name: str,
    width: int,
    rows: list[list[object]],
) -> None:
    """Replace the data rows of an export-only sheet."""

    old = await client.get_values(sheet_name)
    await _write_sheet(client, sheet_name, width, list(rows), old_length=len(old))


def _edited_since(spec: _SheetSpec, snapshot: list[list[object]], offset: int, row: list[object]) -> bool:
    """Did the user change this row after the import read it? (ID column excluded:
    the import itself may have stamped it.)"""

    if offset >= len(snapshot):
        return True  # the row did not exist at import time

    def cells(values: list[object]) -> list[str]:
        padded = list(values) + [""] * spec.user_columns
        return [str(cell).strip() for cell in padded[1 : spec.user_columns]]

    return cells(snapshot[offset]) != cells(row)


async def _export_merged(
    session: AsyncSession,
    client: GoogleSheetsClient,
    spec: _SheetSpec,
    preserve: Collection[RowKey],
    blank_rows: Collection[int],
    import_snapshot: list[list[object]] | None = None,
) -> None:
    """Refresh known rows from SQLite; every row that is not ours is left alone.

    Rows keep their exact positions, blank rows included: the user may keep
    notes in columns to the right of the table, and those must stay next to
    their record. Only the table's own columns are written, and only for rows
    of known records: invalid, unknown, duplicate and blank rows are skipped, so
    the user's cells (including their types and formats) are never rewritten.
    ``blank_rows`` are rows whose record was just deleted at the user's request:
    their table cells are cleared. Where the user keeps the yes/no column as a
    checkbox the value stays a boolean. A row the user edited after the import read
    the sheet (``import_snapshot``) is not written either: the edit is picked up by
    the next sync instead of being overwritten.
    """

    records = await spec.list_all(session)
    by_id = {record.id: record for record in records}
    by_key = {record.sheet_key: record for record in records if record.sheet_key}
    width = len(spec.headers)

    current = await client.get_values(spec.sheet)
    raw_current = await client.get_values(spec.sheet, raw=True)
    rows: list[list[object] | None] = []
    placed: set[int] = set()
    for offset, row in enumerate(current[1:], start=1):  # row 1 is the header
        if offset + 1 in blank_rows:
            rows.append([""] * width)  # deleted at the user's request: clear it
            continue
        if _is_empty_row(row, width):
            rows.append(None)  # blank row: keep its place so the rows below do not shift
            continue
        key = _safe_row_key(row)
        record = by_id.get(key) if isinstance(key, int) else by_key.get(key)
        if record is None or record.id in placed:
            rows.append(None)  # unknown, new or duplicate row: not ours to write
            continue
        placed.add(record.id)  # this record has its row, even if the row is invalid
        if key in preserve:
            rows.append(None)  # the user's edit failed import: leave it exactly as typed
            continue
        if import_snapshot is not None and _edited_since(spec, import_snapshot, offset, row):
            rows.append(None)  # edited while the sync was running: do not overwrite it
            continue
        new_row = spec.to_row(record)
        raw_row = raw_current[offset] if offset < len(raw_current) else []
        if spec.active_column < len(raw_row) and isinstance(raw_row[spec.active_column], bool):
            new_row[spec.active_column] = bool(record.is_active)  # a checkbox stays a checkbox
        rows.append(new_row)
    rows.extend(spec.to_row(record) for record in records if record.id not in placed)
    await _write_sheet(client, spec.sheet, width, rows, old_length=len(current))


async def export_events(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    preserve: Collection[RowKey] = (),
    sheet: str | None = None,
    blank_rows: Collection[int] = (),
    import_snapshot: list[list[object]] | None = None,
) -> None:
    """Merge all events (including inactive) into the Events sheet.

    ``preserve`` holds the keys of rows whose import failed; they stay as typed.
    ``sheet`` is the sheet's current tab title (default: the legacy name).
    """

    await _export_merged(session, client, _on_sheet(_EVENTS, sheet), preserve, blank_rows, import_snapshot)


async def export_expenses(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    preserve: Collection[RowKey] = (),
    sheet: str | None = None,
    blank_rows: Collection[int] = (),
    import_snapshot: list[list[object]] | None = None,
) -> None:
    """Merge all expenses (including inactive) into the Expenses sheet."""

    await _export_merged(
        session, client, _on_sheet(_EXPENSES, sheet), preserve, blank_rows, import_snapshot
    )


async def export_reminders(
    session: AsyncSession, client: GoogleSheetsClient, *, sheet: str | None = None
) -> None:
    """Overwrite the data rows of the Reminders sheet (export-only sheet)."""

    rows = [reminder_to_row(r) for r in await reminders.list_reminders(session)]
    await _overwrite_sheet(client, sheet or "Reminders", len(REMINDER_HEADERS), rows)


INBOX_SHEET_LIMIT = 200  # the newest letters shown in the Inbox sheet


async def export_inbox(
    session: AsyncSession, client: GoogleSheetsClient, *, sheet: str | None = None
) -> None:
    """Overwrite the Inbox sheet with the newest received letters (export-only)."""

    result = await session.execute(
        select(InboxMessage).order_by(InboxMessage.id.desc()).limit(INBOX_SHEET_LIMIT)
    )
    rows = [inbox_to_row(message) for message in result.scalars()]
    await _overwrite_sheet(client, sheet or "Inbox", len(INBOX_HEADERS), rows)


async def export_senders(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    preserve: Collection[RowKey] = (),
    sheet: str | None = None,
    blank_rows: Collection[int] = (),
    import_snapshot: list[list[object]] | None = None,
) -> None:
    """Merge the allowed senders into the Email sheet."""

    await _export_merged(
        session, client, _on_sheet(_SENDERS, sheet), preserve, blank_rows, import_snapshot
    )


def _on_sheet(spec: _SheetSpec, sheet: str | None) -> _SheetSpec:
    return spec if sheet is None else replace(spec, sheet=sheet)


# --- Import -----------------------------------------------------------------

async def _ensure_outer_transaction(session: AsyncSession) -> None:
    """Flush pending changes and force a real SQLite transaction.

    SQLite implements transactions with ``SAVEPOINT`` too: ``SAVEPOINT`` begins
    a transaction when none is active, so the ``begin_nested()`` savepoint would
    otherwise become the *outermost* transaction and its ``RELEASE`` would
    COMMIT (not just release). Emitting an explicit ``BEGIN`` first makes the
    nested savepoint a true nested transaction, so import changes can be rolled
    back independently of the caller's transaction.
    """

    await session.flush()
    connection = await session.connection()
    try:
        await connection.exec_driver_sql("BEGIN")
    except OperationalError:
        # A transaction is already open (the flush emitted writes, or the caller
        # had already begun one) — the savepoint will nest correctly.
        pass


@dataclass
class _PlannedRow:
    row_number: int
    row: list[object]
    key: RowKey
    record: Any
    fields: dict
    delete: bool = False


async def _find_record(session: AsyncSession, spec: _SheetSpec, key: RowKey) -> Any:
    """Resolve a row key to its record; an unknown int ID is an error."""

    if key is None:
        return None
    if isinstance(key, int):
        record = await session.get(spec.model, key)
        if record is None:
            raise ValueError(f"Unknown {spec.label} ID: {key}")
        return record
    result = await session.execute(select(spec.model).where(spec.model.sheet_key == key))
    return result.scalar_one_or_none()  # pending key not created yet -> create


async def _plan_rows(
    session: AsyncSession,
    spec: _SheetSpec,
    data: list[list[object]],
    normalized: list[list[object]],
    cell_errors: dict[int, str],
) -> tuple[list[_PlannedRow], list[SheetValidationError]]:
    """Parse and validate every data row; collect errors instead of stopping.

    ``data`` is the sheet as displayed (used for keys and the stamping check);
    ``normalized`` holds the same rows with real values in the date/number
    columns (what is parsed); ``cell_errors`` rows are rejected outright.
    """

    planned: list[_PlannedRow] = []
    errors: list[SheetValidationError] = []
    seen: set[int | str] = set()
    for offset, row in enumerate(data[1:], start=1):  # row 1 is the header
        if _is_empty_row(row, len(spec.headers)):
            continue
        row_number = offset + 1
        key = _safe_row_key(row)
        try:
            if row_number in cell_errors:
                raise ValueError(cell_errors[row_number])
            fields = spec.parse(normalized[offset])
            fields.pop("id")
            delete = bool(fields.pop("delete", False))
            if key is not None:
                if key in seen:
                    raise ValueError(f"Duplicate ID: {key}")
                seen.add(key)
            record = await _find_record(session, spec, key)
            if delete and record is None:
                raise ValueError("Nothing to delete: this row has no saved record yet")
            if record is None:
                spec.require_new(fields)
        except ValueError as exc:
            errors.append(SheetValidationError(row_number, str(exc), key=key))
            continue
        planned.append(_PlannedRow(row_number, row, key, record, fields, delete))
    return planned, errors


async def _stamp_pending_keys(
    client: GoogleSheetsClient, spec: _SheetSpec, new_rows: list[_PlannedRow]
) -> list[_PlannedRow]:
    """Write a pending key into the ID cell of each new row before creating it.

    The sheet is re-read first and a row is only stamped if it is still exactly
    where and what it was, so a concurrent user edit can never receive another
    row's key. Skipped rows are simply picked up by the next sync.
    """

    if not new_rows:
        return []
    current = await client.get_values(spec.sheet)
    stamped: list[_PlannedRow] = []
    for item in new_rows:
        index = item.row_number - 1
        if index >= len(current) or list(current[index]) != list(item.row):
            logger.info("%s row %d changed during sync; retrying later", spec.sheet, item.row_number)
            continue
        item.key = new_pending_key()
        await client.update_values(f"{spec.sheet}!A{item.row_number}", [[item.key]])
        stamped.append(item)
    return stamped


async def _import_sheet(
    session: AsyncSession,
    client: GoogleSheetsClient,
    spec: _SheetSpec,
    reports: list[RowReport] | None = None,
    snapshot: list[list[object]] | None = None,
) -> list[SheetValidationError]:
    data = await client.get_values(spec.sheet)
    if snapshot is not None:
        snapshot.extend(data)
    normalized, cell_errors = data, {}
    if spec.numeric_columns:
        raw = await client.get_values(spec.sheet, raw=True)
        normalized, cell_errors = _normalize_rows(data, raw, spec.numeric_columns)
    planned, errors = await _plan_rows(session, spec, data, normalized, cell_errors)

    ready = [item for item in planned if item.key is not None]
    # Only genuinely new rows are stamped (a deletion always has a saved record).
    ready += await _stamp_pending_keys(client, spec, [i for i in planned if i.key is None])
    ready.sort(key=lambda item: item.row_number)

    accepted: list[RowReport] = []
    await _ensure_outer_transaction(session)
    for item in ready:
        try:
            async with session.begin_nested():
                if item.delete:
                    record_id = item.record.id
                    await spec.delete(session, item.record)
                    accepted.append(RowReport(item.row_number, record_id, deleted=True))
                    logger.info(
                        "Deleted %s %s at the user's request (sheet row %d)",
                        spec.label, record_id, item.row_number,
                    )
                    continue
                if item.record is None:
                    record = await spec.create(session, {**item.fields, "sheet_key": item.key})
                else:
                    record = item.record
                    await spec.update(session, record, item.fields)
                accepted.append(RowReport(item.row_number, record.id))
        except ValueError as exc:
            # Domain validation rejected the row; its savepoint was rolled back.
            errors.append(SheetValidationError(item.row_number, str(exc), key=item.key))

    errors.sort(key=lambda error: error.row_number)
    for error in errors:
        logger.warning("Sheet %s: %s", spec.sheet, error)
    if reports is not None:
        reports.extend(accepted)
        reports.extend(RowReport(e.row_number, error=e.message) for e in errors)
        reports.sort(key=lambda report: report.row_number)
    return errors


async def import_events(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    sheet: str | None = None,
    reports: list[RowReport] | None = None,
    snapshot: list[list[object]] | None = None,
) -> list[SheetValidationError]:
    """Create/update/delete events from the Events sheet; return per-row errors.

    Valid rows are applied even when other rows are invalid. Nothing is
    committed: on success the changes remain pending for the caller. When a
    ``reports`` list is given, one :class:`RowReport` per sheet row is added.
    """

    return await _import_sheet(session, client, _on_sheet(_EVENTS, sheet), reports, snapshot)


async def import_expenses(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    today: date | None = None,
    sheet: str | None = None,
    reports: list[RowReport] | None = None,
    snapshot: list[list[object]] | None = None,
) -> list[SheetValidationError]:
    """Create/update/delete expenses from the Expenses sheet (see :func:`import_events`)."""

    spec = _EXPENSES if today is None else replace(_EXPENSES, update=_expense_updater(today))
    return await _import_sheet(session, client, _on_sheet(spec, sheet), reports, snapshot)


async def import_senders(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    sheet: str | None = None,
    reports: list[RowReport] | None = None,
    snapshot: list[list[object]] | None = None,
) -> list[SheetValidationError]:
    """Create/update/delete allowed Gmail senders from the Email sheet."""

    return await _import_sheet(session, client, _on_sheet(_SENDERS, sheet), reports, snapshot)


# --- record type specs --------------------------------------------------------

async def _create_event(session: AsyncSession, fields: dict) -> Event:
    sheet_key = fields.pop("sheet_key")
    offsets = fields.pop("reminder_offsets", None)
    event = await events.create_event(session, **fields)
    event.sheet_key = sheet_key
    if offsets is not None:
        await events.update_event(session, event, reminder_offsets=offsets)
    await session.flush()
    return event


async def _update_event(session: AsyncSession, event: Event, fields: dict) -> None:
    await events.update_event(session, event, **fields)


async def _create_expense(session: AsyncSession, fields: dict) -> RecurringExpense:
    sheet_key = fields.pop("sheet_key")
    period = fields.pop("period", ExpensePeriod.MONTHLY)
    expense = await expenses.create_expense(session, period=period, **fields)
    expense.sheet_key = sheet_key
    await session.flush()
    return expense


def _expense_updater(today: date | None):
    async def _update(session: AsyncSession, expense: RecurringExpense, fields: dict) -> None:
        await expenses.update_expense(session, expense, today=today, **fields)

    return _update


async def _list_events(session: AsyncSession) -> list[Event]:
    return await events.list_events(session, active_only=False)


async def _list_expenses(session: AsyncSession) -> list[RecurringExpense]:
    return await expenses.list_expenses(session, active_only=False)


async def _delete_event(session: AsyncSession, event: Event) -> None:
    await events.delete_event(session, event)


async def _delete_expense(session: AsyncSession, expense: RecurringExpense) -> None:
    await expenses.delete_expense(session, expense)


async def _create_sender(session: AsyncSession, fields: dict) -> AllowedSender:
    sheet_key = fields.pop("sheet_key")
    sender = await senders.create_sender(session, **fields)
    sender.sheet_key = sheet_key
    await session.flush()
    return sender


async def _update_sender(session: AsyncSession, sender: AllowedSender, fields: dict) -> None:
    await senders.update_sender(session, sender, **fields)


_SENDERS = _SheetSpec(
    sheet="Email",
    label="sender",
    headers=SENDER_HEADERS,
    model=AllowedSender,
    parse=parse_sender_row,
    require_new=require_new_sender_fields,
    to_row=sender_to_row,
    list_all=senders.list_senders,
    create=_create_sender,
    update=_update_sender,
    delete=senders.delete_sender,
    numeric_columns={},
    active_column=2,
    user_columns=3,
)

_EVENTS = _SheetSpec(
    sheet="Events",
    label="event",
    headers=EVENT_HEADERS,
    model=Event,
    parse=parse_event_row,
    require_new=require_new_event_fields,
    to_row=event_to_row,
    list_all=_list_events,
    create=_create_event,
    update=_update_event,
    delete=_delete_event,
    numeric_columns={2: "date"},
    active_column=6,
    user_columns=7,
)

_EXPENSES = _SheetSpec(
    sheet="Expenses",
    label="expense",
    headers=EXPENSE_HEADERS,
    model=RecurringExpense,
    parse=parse_expense_row,
    require_new=require_new_expense_fields,
    to_row=expense_to_row,
    list_all=_list_expenses,
    create=_create_expense,
    update=_expense_updater(None),
    delete=_delete_expense,
    numeric_columns={2: "number", 5: "number", 7: "date", 9: "number"},
    active_column=8,
    user_columns=10,
)
