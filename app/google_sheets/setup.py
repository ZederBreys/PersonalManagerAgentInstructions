"""Workbook initialization: ensure the expected sheets and headers exist.

This module only *creates* missing sheets and writes headers where absent. It
never deletes, re-creates or clears existing content, so user data is preserved.
"""

from __future__ import annotations

from app.google_sheets.client import GoogleSheetsClient
from app.google_sheets.mappers import (
    EVENT_HEADERS,
    EXPENSE_HEADERS,
    REMINDER_HEADERS,
    SETTINGS_HEADERS,
)

# Sheets with a defined, app-owned header row.
_HEADERED_SHEETS = {
    "Events": EVENT_HEADERS,
    "Expenses": EXPENSE_HEADERS,
    "Reminders": REMINDER_HEADERS,
    "Settings": SETTINGS_HEADERS,
}

# Sheets that exist for future stages but are not managed yet (no header row).
_PLACEHOLDER_SHEETS = ["Inbox", "Email"]

ALL_SHEETS = list(_HEADERED_SHEETS) + _PLACEHOLDER_SHEETS


async def ensure_workbook(client: GoogleSheetsClient) -> None:
    """Create any missing sheets and write headers where they are absent.

    Existing sheets are left untouched (their content is preserved); only a
    missing sheet is added, and only a sheet whose first cell is empty gets a
    header row written.
    """
    existing = set(await client.get_sheet_titles())

    for title in ALL_SHEETS:
        if title not in existing:
            await client.add_sheet(title)
            existing.add(title)

    for title, headers in _HEADERED_SHEETS.items():
        rows = await client.get_values(f"{title}!1:1")
        has_header = bool(rows and rows[0] and any(cell not in (None, "") for cell in rows[0]))
        if not has_header:
            await client.update_values(f"{title}!A1", [headers])
