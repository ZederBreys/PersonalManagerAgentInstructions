"""Regression: concurrent jobs must never use one Sheets connection from two threads.

Production crashed with ``free(): corrupted unsorted chunks`` when the startup
``sheets_sync`` and ``daily_reminders`` jobs called the shared
``GoogleSheetsClient`` at the same time: each call ran in its own worker thread
and OpenSSL read the same TLS connection concurrently. The fake service below is
deliberately thread-unsafe like the real one: overlapping calls are recorded
(where the real client would corrupt memory).
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from datetime import date

from app import db, events
from app.config import Settings
from app.google_sheets.client import GoogleSheetsClient
from app.google_sheets.mappers import EVENT_HEADERS, event_to_row
from app.jobs import Services, SheetsSyncError, daily_reminders, sheets_sync
from app.models.event import Event


class _Request:
    def __init__(self, service: "ThreadUnsafeService", action) -> None:
        self._service = service
        self._action = action

    def execute(self):
        return self._service.run(self._action)


class ThreadUnsafeService:
    """Fake Sheets ``service`` that detects concurrent use like a single TLS socket."""

    def __init__(self, sheets: dict[str, list[list[object]]] | None = None) -> None:
        self.sheets = {k: [list(r) for r in v] for k, v in (sheets or {}).items()}
        self._guard = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.overlaps = 0

    def run(self, action):
        with self._guard:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if self._active > 1:
                self.overlaps += 1
        try:
            time.sleep(0.005)  # widen the window, like a network round trip
            return action()
        finally:
            with self._guard:
                self._active -= 1

    # --- resource chain used by GoogleSheetsClient ---
    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, *, spreadsheetId, range=None, fields=None, valueRenderOption=None):
        if fields is not None:  # spreadsheets().get(...): sheet titles
            return _Request(self, lambda: {"sheets": [{"properties": {"title": t}} for t in self.sheets]})
        return _Request(self, lambda: {"values": self._read(range)})

    def update(self, *, spreadsheetId, range, valueInputOption, body):
        return _Request(self, lambda: self._write(range, body["values"]))

    def clear(self, *, spreadsheetId, range, body):
        return _Request(self, lambda: self._clear(range))

    def batchUpdate(self, *, spreadsheetId, body):
        def add() -> None:
            for request in body["requests"]:
                self.sheets.setdefault(request["addSheet"]["properties"]["title"], [])

        return _Request(self, add)

    def _read(self, range_name: str) -> list[list[object]]:
        name, _, cells = range_name.partition("!")
        rows = [list(r) for r in self.sheets.get(name, [])]
        while rows and not any(str(c).strip() for c in rows[-1]):
            rows.pop()
        return rows[:1] if cells == "1:1" else rows

    def _write(self, range_name: str, values: list[list[object]]) -> None:
        name, _, cells = range_name.partition("!")
        start = int(re.fullmatch(r"A(\d+)", cells).group(1)) - 1 if cells else 0
        rows = self.sheets.setdefault(name, [])
        for i, row in enumerate(values):
            while len(rows) <= start + i:
                rows.append([])
            target = rows[start + i]
            for j, value in enumerate(row):
                while len(target) <= j:
                    target.append("")
                target[j] = value

    def _clear(self, range_name: str) -> None:
        name, _, cells = range_name.partition("!")
        first, last = (int(n) for n in re.findall(r"\d+", cells))
        rows = self.sheets.get(name, [])
        for index in range(first - 1, min(last, len(rows))):
            rows[index] = []


def _client(service: ThreadUnsafeService) -> GoogleSheetsClient:
    return GoogleSheetsClient("unused.json", "spreadsheet-id", service=service)


def test_fake_service_detects_unserialized_concurrent_use() -> None:
    """Sanity check: without the client's lock the detector does see overlaps."""

    service = ThreadUnsafeService({"Events": [["ID"]]})

    async def _run() -> None:
        calls = [
            asyncio.to_thread(service.values().get(spreadsheetId="x", range="Events").execute)
            for _ in range(8)
        ]
        await asyncio.gather(*calls)

    asyncio.run(_run())
    assert service.overlaps > 0


def test_client_serializes_concurrent_calls() -> None:
    service = ThreadUnsafeService({"Events": [["ID"]], "Expenses": [["ID"]]})
    client = _client(service)

    async def _run() -> None:
        calls = []
        for _ in range(10):
            calls += [
                client.get_values("Events"),
                client.update_values("Expenses!A2", [["x"]]),
                client.get_sheet_titles(),
            ]
        await asyncio.gather(*calls)

    asyncio.run(_run())
    assert service.max_active == 1
    assert service.overlaps == 0


def test_startup_jobs_with_invalid_row_do_not_share_connection_concurrently(
    schema: None,
) -> None:
    """The production startup scenario: both jobs at once, with a bad date row."""

    async def _create() -> Event:
        async with db.get_session() as session:
            event = await events.create_event(session, name="Как было", next_date=date(2026, 12, 1))
            await session.commit()
            return event

    existing = asyncio.run(_create())
    edited = event_to_row(existing)
    edited[1] = "Изменено"
    edited[2] = "24.08.2025"  # invalid date on an existing record
    service = ThreadUnsafeService(
        {
            "Events": [
                list(EVENT_HEADERS),
                [str(c) for c in edited],
                ["", "Новая встреча", "2026-12-24", "none", "0", "", ""],
                ["", "Плохая дата", "24.08.2025", "", "", "", ""],
            ]
        }
    )
    services = Services(settings=Settings(_env_file=None), sheets=_client(service))

    async def _run() -> list:
        return await asyncio.gather(
            daily_reminders(services, today=date(2026, 10, 3)),
            sheets_sync(services),
            return_exceptions=True,
        )

    results = asyncio.run(_run())

    # Both jobs finished normally, reporting the invalid rows (no crash, no hang).
    assert all(isinstance(r, SheetsSyncError) for r in results), results
    assert service.overlaps == 0 and service.max_active == 1

    async def _events() -> list[Event]:
        async with db.get_session() as session:
            return await events.list_events(session, active_only=False)

    stored = {e.name: e for e in asyncio.run(_events())}
    assert set(stored) == {"Как было", "Новая встреча"}  # existing untouched, valid row created
    assert stored["Как было"].next_date == date(2026, 12, 1)

    rows = [r for r in service.sheets["Events"][1:] if any(str(c).strip() for c in r)]
    assert rows[0][:3] == [str(existing.id), "Изменено", "24.08.2025"]  # kept as typed
    assert rows[1][:2] == [stored["Новая встреча"].id, "Новая встреча"]  # got its ID
    assert rows[2][:3] == ["", "Плохая дата", "24.08.2025"]  # kept as typed
