"""Workbook initialization: find (or create) the sheets and fill missing headers.

Sheets are identified by a hidden marker (Sheets "developer metadata") that the
application puts on each sheet it manages, never by tab name or position. The
user can therefore rename or reorder tabs freely. Existing sheets named like the
old defaults (``Events``, ``Expenses``, ...) are adopted and marked once.

Column headers are the user's to rename: columns are identified by position, so
only *empty* header cells are filled (and the old default ``Статус`` is replaced
once by ``Активно``). Nothing is ever deleted, cleared or re-created.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.google_sheets.client import GoogleSheetsClient
from app.google_sheets.mappers import (
    EVENT_HEADERS,
    EXPENSE_HEADERS,
    INBOX_HEADERS,
    REMINDER_HEADERS,
    SENDER_HEADERS,
)

logger = logging.getLogger(__name__)

ROLE_EVENTS = "events"
ROLE_EXPENSES = "expenses"
ROLE_REMINDERS = "reminders"
ROLE_INBOX = "inbox"
ROLE_EMAIL = "email"

# role -> (default title, default headers or None for sheets without a header row)
_ROLES: dict[str, tuple[str, list[str] | None]] = {
    ROLE_EVENTS: ("Events", EVENT_HEADERS),
    ROLE_EXPENSES: ("Expenses", EXPENSE_HEADERS),
    ROLE_REMINDERS: ("Reminders", REMINDER_HEADERS),
    ROLE_INBOX: ("Inbox", INBOX_HEADERS),  # export only: the letters the bot received
    ROLE_EMAIL: ("Email", SENDER_HEADERS),  # the addresses the bot may read
}

# Old default header texts that are replaced once by the new default.
_LEGACY_HEADERS: dict[tuple[str, int], set[str]] = {(ROLE_EVENTS, 6): {"Статус"}}


@dataclass(frozen=True)
class SheetRef:
    """Where a role currently lives: its stable id and its current tab title."""

    role: str
    title: str
    sheet_id: int


SheetLayout = dict[str, SheetRef]


def _column_letter(index: int) -> str:
    return chr(ord("A") + index)


def _unique_title(wanted: str, taken: set[str]) -> str:
    title, number = wanted, 2
    while title in taken:
        title, number = f"{wanted} {number}", number + 1
    return title


async def _resolve_sheets(client: GoogleSheetsClient) -> SheetLayout:
    sheets = await client.get_sheets()
    roles = await client.get_sheet_roles()
    by_id = {sheet.sheet_id: sheet for sheet in sheets}
    layout: SheetLayout = {}

    for role, (default_title, _headers) in _ROLES.items():
        found = next((by_id[sid] for sid, r in roles.items() if r == role and sid in by_id), None)
        if found is None:
            # Not marked yet: adopt an unmarked sheet with the default title, else create one.
            found = next(
                (s for s in sheets if s.title == default_title and s.sheet_id not in roles), None
            )
            if found is None:
                await client.add_sheet(_unique_title(default_title, {s.title for s in sheets}))
                sheets = await client.get_sheets()
                known = {s.sheet_id for s in by_id.values()}
                found = next(s for s in sheets if s.sheet_id not in known and s.sheet_id not in roles)
                by_id = {sheet.sheet_id: sheet for sheet in sheets}
            await client.tag_sheet(found.sheet_id, role)
            roles[found.sheet_id] = role
        layout[role] = SheetRef(role, found.title, found.sheet_id)
    return layout


async def _fill_missing_headers(client: GoogleSheetsClient, layout: SheetLayout) -> None:
    headered = [(role, headers) for role, (_t, headers) in _ROLES.items() if headers]
    first_rows = await client.batch_get([f"{layout[role].title}!1:1" for role, _ in headered])
    for (role, headers), rows in zip(headered, first_rows):
        current = [str(cell).strip() for cell in (rows[0] if rows else [])]
        for index, default in enumerate(headers):
            text = current[index] if index < len(current) else ""
            if text and text not in _LEGACY_HEADERS.get((role, index), set()):
                continue  # the user's own header text: never touched
            await client.update_values(
                f"{layout[role].title}!{_column_letter(index)}1", [[default]]
            )


async def ensure_workbook(client: GoogleSheetsClient) -> SheetLayout:
    """Resolve every managed sheet (by hidden marker) and fill missing headers.

    Returns ``{role: SheetRef}`` with the sheets' *current* titles; callers must
    use these titles instead of hard-coded names.
    """

    layout = await _resolve_sheets(client)
    await _fill_missing_headers(client, layout)
    return layout
