"""Export (SQLite -> Sheets) and Import (Sheets -> SQLite) orchestration.

SQLite is the single source of truth; Google Sheets is a human-facing view.
Export overwrites the app-owned range of a specific sheet. Import reads the
sheet, validates every row first, and only then applies changes through the
existing domain functions.

The caller owns the top-level transaction: import applies its changes inside a
nested transaction (SAVEPOINT) and never commits, so a failed import rolls back
only its own changes and never commits unrelated pending state.
"""

from __future__ import annotations

from datetime import date

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
    parse_event_row,
    parse_expense_row,
    reminder_to_row,
)


class SheetValidationError(ValueError):
    """A validation problem for a specific sheet row (1-based sheet number)."""

    def __init__(self, row_number: int, message: str) -> None:
        self.row_number = row_number
        self.message = message
        super().__init__(f"Row {row_number}: {message}")


def _is_header_row(row: list[object], headers: list[str]) -> bool:
    return bool(row) and isinstance(row[0], str) and row[0].strip() == headers[0]


def _is_empty_row(row: list[object]) -> bool:
    return all(cell is None or (isinstance(cell, str) and not cell.strip()) for cell in row)


def _collect_updates(
    data: list[list[object]],
    headers: list[str],
    parse_fn,
) -> tuple[list[tuple[int, dict]], list[SheetValidationError]]:
    """Parse all data rows into ``(row_number, fields)`` and collect errors.

    The header row (when present) is skipped; fully-empty rows are ignored.
    """
    updates: list[tuple[int, dict]] = []
    errors: list[SheetValidationError] = []

    start = 1 if (data and _is_header_row(data[0], headers)) else 0
    for offset, row in enumerate(data[start:], start=start):
        if _is_empty_row(row):
            continue
        row_number = offset + 1
        try:
            fields = parse_fn(row)
        except ValueError as exc:
            errors.append(SheetValidationError(row_number, str(exc)))
            continue
        updates.append((row_number, fields))

    return updates, errors


# --- Export -----------------------------------------------------------------

def _column_letter(index: int) -> str:
    """Return the spreadsheet column letter for a 0-based column index (A..Z)."""

    return chr(ord("A") + index)


async def _overwrite_sheet(
    client: GoogleSheetsClient,
    sheet_name: str,
    headers: list[str],
    rows: list[list[object]],
) -> None:
    """Replace the app-owned sheet with ``headers`` + ``rows``.

    Writes the new content first (a single atomic ``values.update`` over the
    contiguous range), then clears only the stale tail left behind when the
    previous version had more rows. Writing before clearing means a failed
    write never erases the previous data.
    """

    new_values = [headers, *rows]
    old = await client.get_values(sheet_name)
    await client.update_values(sheet_name, new_values)
    if len(old) > len(new_values):
        last_col = _column_letter(len(headers) - 1)
        await client.clear(f"{sheet_name}!A{len(new_values) + 1}:{last_col}{len(old)}")


async def export_events(session: AsyncSession, client: GoogleSheetsClient) -> None:
    """Overwrite the Events sheet with all events (including inactive)."""

    rows = [event_to_row(e) for e in await events.list_events(session, active_only=False)]
    await _overwrite_sheet(client, "Events", EVENT_HEADERS, rows)


async def export_expenses(session: AsyncSession, client: GoogleSheetsClient) -> None:
    """Overwrite the Expenses sheet with all expenses (including inactive)."""

    rows = [expense_to_row(e) for e in await expenses.list_expenses(session, active_only=False)]
    await _overwrite_sheet(client, "Expenses", EXPENSE_HEADERS, rows)


async def export_reminders(session: AsyncSession, client: GoogleSheetsClient) -> None:
    """Overwrite the Reminders sheet with all reminders."""

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


async def import_events(
    session: AsyncSession, client: GoogleSheetsClient
) -> list[SheetValidationError]:
    """Read the Events sheet and update existing events by ID.

    Returns a list of validation errors. When the list is non-empty the database
    is left unchanged. Unknown IDs and empty ID cells are errors; events are
    never created from the sheet.

    The caller owns the top-level transaction: this function does not commit.
    Changes are applied inside a nested transaction (SAVEPOINT) so that a
    failure rolls back only this import's changes and never touches any
    pre-existing pending state on the session. On success the changes remain
    pending for the caller to commit.
    """

    data = await client.get_values("Events")
    updates, errors = _collect_updates(data, EVENT_HEADERS, parse_event_row)

    prepared: list[tuple] = []
    for row_number, fields in updates:
        event_id = fields.pop("id")
        event = await events.get_event(session, event_id)
        if event is None:
            errors.append(SheetValidationError(row_number, f"Unknown event ID: {event_id}"))
            continue
        prepared.append((event, fields))

    if errors:
        return errors

    await _ensure_outer_transaction(session)
    async with session.begin_nested():
        for event, fields in prepared:
            await events.update_event(session, event, **fields)

    return []


async def import_expenses(
    session: AsyncSession,
    client: GoogleSheetsClient,
    *,
    today: date | None = None,
) -> list[SheetValidationError]:
    """Read the Expenses sheet and update existing expenses by ID.

    Same transaction semantics as :func:`import_events`: validate all rows
    first, then apply through ``update_expense`` inside a nested transaction,
    leaving the commit to the caller.
    """

    data = await client.get_values("Expenses")
    updates, errors = _collect_updates(data, EXPENSE_HEADERS, parse_expense_row)

    prepared: list[tuple] = []
    for row_number, fields in updates:
        expense_id = fields.pop("id")
        expense = await expenses.get_expense(session, expense_id)
        if expense is None:
            errors.append(SheetValidationError(row_number, f"Unknown expense ID: {expense_id}"))
            continue
        prepared.append((expense, fields))

    if errors:
        return errors

    await _ensure_outer_transaction(session)
    async with session.begin_nested():
        for expense, fields in prepared:
            await expenses.update_expense(session, expense, today=today, **fields)

    return []
