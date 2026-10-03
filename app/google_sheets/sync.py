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
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app import events, expenses, reminders
from app.google_sheets.client import GoogleSheetsClient
from app.google_sheets.mappers import (
    EVENT_HEADERS,
    EXPENSE_HEADERS,
    REMINDER_HEADERS,
    event_to_row,
    expense_to_row,
    new_pending_key,
    parse_event_row,
    parse_expense_row,
    parse_row_key,
    reminder_to_row,
    require_new_event_fields,
    require_new_expense_fields,
)
from app.models.event import Event
from app.models.expense import ExpensePeriod, RecurringExpense

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


def _is_empty_row(row: list[object]) -> bool:
    return all(cell is None or (isinstance(cell, str) and not cell.strip()) for cell in row)


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


# --- Export -----------------------------------------------------------------

def _column_letter(index: int) -> str:
    """Return the spreadsheet column letter for a 0-based column index (A..Z)."""

    return chr(ord("A") + index)


async def _write_sheet(
    client: GoogleSheetsClient,
    sheet_name: str,
    width: int,
    rows: list[list[object]],
    *,
    old_length: int,
) -> None:
    """Write the data ``rows`` from A2 down and clear the stale tail.

    Row 1 (the header) is never written here: its text is the user's to rename.
    Writing before clearing means a failed write never erases previous data.
    Rows are padded to the table width: ``values.update`` leaves cells it is
    not given untouched, so a short row moved onto a longer one would otherwise
    keep the old row's trailing cells.
    """

    if rows:
        padded = [list(row) + [""] * (width - len(row)) for row in rows]
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
    await _write_sheet(client, sheet_name, width, rows, old_length=len(old))


async def _export_merged(
    session: AsyncSession,
    client: GoogleSheetsClient,
    spec: _SheetSpec,
    preserve: Collection[RowKey],
    blank_rows: Collection[int],
) -> None:
    """Refresh known rows from SQLite, keep every other user row untouched.

    Rows keep their exact positions, blank rows included: the user may keep
    notes in columns to the right of the table, and those must stay next to
    their record. Only the table's own columns are written; anything to the
    right is never read back or rewritten. ``blank_rows`` are rows whose record
    was just deleted at the user's request: they are emptied in place.
    """

    records = await spec.list_all(session)
    by_id = {record.id: record for record in records}
    by_key = {record.sheet_key: record for record in records if record.sheet_key}
    width = len(spec.headers)

    current = await client.get_values(spec.sheet)
    rows: list[list[object]] = []
    placed: set[int] = set()
    for offset, row in enumerate(current[1:], start=1):  # row 1 is the header
        if offset + 1 in blank_rows or _is_empty_row(row):
            rows.append([])  # blank row: keep its place so the rows below do not shift
            continue
        key = _safe_row_key(row)
        record = by_id.get(key) if isinstance(key, int) else by_key.get(key)
        if record is None or record.id in placed:
            rows.append(list(row)[:width])  # unknown, invalid, new or duplicate row
            continue
        placed.add(record.id)
        if key in preserve:
            rows.append(list(row)[:width])  # the user's edit failed import: keep it
        else:
            rows.append(spec.to_row(record))
    rows.extend(spec.to_row(record) for record in records if record.id not in placed)
    await _write_sheet(client, spec.sheet, width, rows, old_length=len(current))


async def export_events(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    preserve: Collection[RowKey] = (),
    sheet: str | None = None,
    blank_rows: Collection[int] = (),
) -> None:
    """Merge all events (including inactive) into the Events sheet.

    ``preserve`` holds the keys of rows whose import failed; they stay as typed.
    ``sheet`` is the sheet's current tab title (default: the legacy name).
    """

    await _export_merged(session, client, _on_sheet(_EVENTS, sheet), preserve, blank_rows)


async def export_expenses(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    preserve: Collection[RowKey] = (),
    sheet: str | None = None,
    blank_rows: Collection[int] = (),
) -> None:
    """Merge all expenses (including inactive) into the Expenses sheet."""

    await _export_merged(session, client, _on_sheet(_EXPENSES, sheet), preserve, blank_rows)


async def export_reminders(
    session: AsyncSession, client: GoogleSheetsClient, *, sheet: str | None = None
) -> None:
    """Overwrite the data rows of the Reminders sheet (export-only sheet)."""

    rows = [reminder_to_row(r) for r in await reminders.list_reminders(session)]
    await _overwrite_sheet(client, sheet or "Reminders", len(REMINDER_HEADERS), rows)


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
    session: AsyncSession, spec: _SheetSpec, data: list[list[object]]
) -> tuple[list[_PlannedRow], list[SheetValidationError]]:
    """Parse and validate every data row; collect errors instead of stopping."""

    planned: list[_PlannedRow] = []
    errors: list[SheetValidationError] = []
    seen: set[int | str] = set()
    for offset, row in enumerate(data[1:], start=1):  # row 1 is the header
        if _is_empty_row(row):
            continue
        row_number = offset + 1
        key = _safe_row_key(row)
        try:
            fields = spec.parse(row)
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
) -> list[SheetValidationError]:
    data = await client.get_values(spec.sheet)
    planned, errors = await _plan_rows(session, spec, data)

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
) -> list[SheetValidationError]:
    """Create/update/delete events from the Events sheet; return per-row errors.

    Valid rows are applied even when other rows are invalid. Nothing is
    committed: on success the changes remain pending for the caller. When a
    ``reports`` list is given, one :class:`RowReport` per sheet row is added.
    """

    return await _import_sheet(session, client, _on_sheet(_EVENTS, sheet), reports)


async def import_expenses(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    today: date | None = None,
    sheet: str | None = None,
    reports: list[RowReport] | None = None,
) -> list[SheetValidationError]:
    """Create/update/delete expenses from the Expenses sheet (see :func:`import_events`)."""

    spec = _EXPENSES if today is None else replace(_EXPENSES, update=_expense_updater(today))
    return await _import_sheet(session, client, _on_sheet(spec, sheet), reports)


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
)
