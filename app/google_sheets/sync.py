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


def _is_header_row(row: list[object], headers: list[str]) -> bool:
    return bool(row) and isinstance(row[0], str) and row[0].strip() == headers[0]


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


# --- Export -----------------------------------------------------------------

def _column_letter(index: int) -> str:
    """Return the spreadsheet column letter for a 0-based column index (A..Z)."""

    return chr(ord("A") + index)


async def _write_sheet(
    client: GoogleSheetsClient,
    sheet_name: str,
    headers: list[str],
    rows: list[list[object]],
    *,
    old_length: int,
) -> None:
    """Write ``headers`` + ``rows`` and clear the stale tail.

    Writing before clearing means a failed write never erases previous data.
    Rows are padded to the header width: ``values.update`` leaves cells it is
    not given untouched, so a short row moved onto a longer one would otherwise
    keep the old row's trailing cells.
    """

    width = len(headers)
    new_values = [headers, *(list(row) + [""] * (width - len(row)) for row in rows)]
    await client.update_values(sheet_name, new_values)
    if old_length > len(new_values):
        last_col = _column_letter(len(headers) - 1)
        await client.clear(f"{sheet_name}!A{len(new_values) + 1}:{last_col}{old_length}")


async def _overwrite_sheet(
    client: GoogleSheetsClient,
    sheet_name: str,
    headers: list[str],
    rows: list[list[object]],
) -> None:
    """Replace an export-only sheet with ``headers`` + ``rows``."""

    old = await client.get_values(sheet_name)
    await _write_sheet(client, sheet_name, headers, rows, old_length=len(old))


async def _export_merged(
    session: AsyncSession,
    client: GoogleSheetsClient,
    spec: _SheetSpec,
    preserve: Collection[RowKey],
) -> None:
    """Refresh known rows from SQLite, keep every other user row untouched."""

    records = await spec.list_all(session)
    by_id = {record.id: record for record in records}
    by_key = {record.sheet_key: record for record in records if record.sheet_key}

    current = await client.get_values(spec.sheet)
    start = 1 if (current and _is_header_row(current[0], spec.headers)) else 0
    rows: list[list[object]] = []
    placed: set[int] = set()
    for row in current[start:]:
        if _is_empty_row(row):
            continue
        key = _safe_row_key(row)
        record = by_id.get(key) if isinstance(key, int) else by_key.get(key)
        if record is None or record.id in placed:
            rows.append(list(row))  # unknown, invalid, new or duplicate row
            continue
        placed.add(record.id)
        if key in preserve:
            rows.append(list(row))  # the user's edit failed import: keep it
        else:
            rows.append(spec.to_row(record))
    rows.extend(spec.to_row(record) for record in records if record.id not in placed)
    await _write_sheet(client, spec.sheet, spec.headers, rows, old_length=len(current))


async def export_events(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    preserve: Collection[RowKey] = (),
) -> None:
    """Merge all events (including inactive) into the Events sheet.

    ``preserve`` holds the keys of rows whose import failed; they stay as typed.
    """

    await _export_merged(session, client, _EVENTS, preserve)


async def export_expenses(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    preserve: Collection[RowKey] = (),
) -> None:
    """Merge all expenses (including inactive) into the Expenses sheet."""

    await _export_merged(session, client, _EXPENSES, preserve)


async def export_reminders(session: AsyncSession, client: GoogleSheetsClient) -> None:
    """Overwrite the Reminders sheet with all reminders (export-only sheet)."""

    rows = [reminder_to_row(r) for r in await reminders.list_reminders(session)]
    await _overwrite_sheet(client, "Reminders", REMINDER_HEADERS, rows)


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
    start = 1 if (data and _is_header_row(data[0], spec.headers)) else 0
    for offset, row in enumerate(data[start:], start=start):
        if _is_empty_row(row):
            continue
        row_number = offset + 1
        key = _safe_row_key(row)
        try:
            fields = spec.parse(row)
            fields.pop("id")
            if key is not None:
                if key in seen:
                    raise ValueError(f"Duplicate ID: {key}")
                seen.add(key)
            record = await _find_record(session, spec, key)
            if record is None:
                spec.require_new(fields)
        except ValueError as exc:
            errors.append(SheetValidationError(row_number, str(exc), key=key))
            continue
        planned.append(_PlannedRow(row_number, row, key, record, fields))
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
    session: AsyncSession, client: GoogleSheetsClient, spec: _SheetSpec
) -> list[SheetValidationError]:
    data = await client.get_values(spec.sheet)
    planned, errors = await _plan_rows(session, spec, data)

    ready = [item for item in planned if item.key is not None]
    ready += await _stamp_pending_keys(client, spec, [i for i in planned if i.key is None])
    ready.sort(key=lambda item: item.row_number)

    await _ensure_outer_transaction(session)
    for item in ready:
        try:
            async with session.begin_nested():
                if item.record is None:
                    await spec.create(session, {**item.fields, "sheet_key": item.key})
                else:
                    await spec.update(session, item.record, item.fields)
        except ValueError as exc:
            # Domain validation rejected the row; its savepoint was rolled back.
            errors.append(SheetValidationError(item.row_number, str(exc), key=item.key))

    errors.sort(key=lambda error: error.row_number)
    for error in errors:
        logger.warning("Sheet %s: %s", spec.sheet, error)
    return errors


async def import_events(
    session: AsyncSession, client: GoogleSheetsClient
) -> list[SheetValidationError]:
    """Create/update events from the Events sheet; return per-row errors.

    Valid rows are applied even when other rows are invalid. Nothing is
    committed: on success the changes remain pending for the caller.
    """

    return await _import_sheet(session, client, _EVENTS)


async def import_expenses(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    today: date | None = None,
) -> list[SheetValidationError]:
    """Create/update expenses from the Expenses sheet (see :func:`import_events`)."""

    spec = _EXPENSES if today is None else replace(_EXPENSES, update=_expense_updater(today))
    return await _import_sheet(session, client, spec)


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
)
