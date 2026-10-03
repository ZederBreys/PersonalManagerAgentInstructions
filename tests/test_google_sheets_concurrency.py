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
import threading
import time
from datetime import date

from app import db, events
from app.config import Settings
from app.google_sheets.client import GoogleSheetsClient
from app.google_sheets.mappers import EVENT_HEADERS, event_to_row
from app.jobs import Services, SheetsSyncError, daily_reminders, sheets_sync
from app.models.event import Event
from tests.test_google_sheets_two_way import SheetStore


class _Request:
    def __init__(self, service: "ThreadUnsafeService", action) -> None:
        self._service = service
        self._action = action

    def execute(self):
        return self._service.run(self._action)


def _plain(range_name: str) -> str:
    """``'Events'!A2`` -> ``Events!A2`` (the client always quotes the tab title)."""

    title, bang, cells = range_name.rpartition("!")
    if not bang:
        title, cells = range_name, ""
    assert title.startswith("'") and title.endswith("'"), range_name
    title = title[1:-1].replace("''", "'")
    return f"{title}!{cells}" if bang else title


class ThreadUnsafeService:
    """Fake Google service that detects concurrent use like a single TLS socket.

    Speaks the googleapiclient resource-chain API the real client uses; the
    spreadsheet behind it is the shared in-memory ``SheetStore``.
    """

    def __init__(self, sheets: dict[str, list[list[object]]] | None = None) -> None:
        self.store = SheetStore(sheets)
        self._guard = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.overlaps = 0

    @property
    def sheets(self) -> dict[str, list[list[object]]]:
        return self.store.sheets

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

    def developerMetadata(self):  # noqa: N802 - Google's name
        return self

    def get(self, *, spreadsheetId, range=None, fields=None, valueRenderOption=None):
        if fields is not None:  # spreadsheets().get(...): the tabs
            return _Request(
                self,
                lambda: {
                    "sheets": [
                        {"properties": {"sheetId": info.sheet_id, "title": info.title}}
                        for info in self.store.sheets_sync()
                    ]
                },
            )
        return _Request(self, lambda: {"values": self.store._rows(_plain(range))})

    def batchGet(self, *, spreadsheetId, ranges, valueRenderOption=None):  # noqa: N802
        return _Request(
            self,
            lambda: {"valueRanges": [{"values": self.store._rows(_plain(r))} for r in ranges]},
        )

    def update(self, *, spreadsheetId, range, valueInputOption, body):
        assert valueInputOption == "USER_ENTERED"
        return _Request(self, lambda: self.store.update_sync(_plain(range), body["values"]))

    def clear(self, *, spreadsheetId, range, body):
        return _Request(self, lambda: self.store.clear_sync(_plain(range)))

    def search(self, *, spreadsheetId, body):
        def found() -> dict:
            return {
                "matchedDeveloperMetadata": [
                    {"developerMetadata": {"metadataValue": role, "location": {"sheetId": sid}}}
                    for sid, role in self.store.roles_sync().items()
                ]
            }

        return _Request(self, found)

    def batchUpdate(self, *, spreadsheetId, body):  # noqa: N802
        def apply() -> None:
            for request in body["requests"]:
                if "addSheet" in request:
                    self.store.add_sheet_sync(request["addSheet"]["properties"]["title"])
                elif "createDeveloperMetadata" in request:
                    metadata = request["createDeveloperMetadata"]["developerMetadata"]
                    self.store.tag_sync(metadata["location"]["sheetId"], metadata["metadataValue"])
                else:
                    self.store.batch_update_sync([request])

        return _Request(self, apply)


def _client(service: ThreadUnsafeService) -> GoogleSheetsClient:
    return GoogleSheetsClient("unused.json", "spreadsheet-id", service=service)


def test_fake_service_detects_unserialized_concurrent_use() -> None:
    """Sanity check: without the client's lock the detector does see overlaps."""

    service = ThreadUnsafeService({"Events": [["ID"]]})

    async def _run() -> None:
        calls = [
            asyncio.to_thread(service.values().get(spreadsheetId="x", range="'Events'").execute)
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
    edited[2] = "31.02.2025"  # invalid date on an existing record
    service = ThreadUnsafeService(
        {
            "Events": [
                list(EVENT_HEADERS),
                [str(c) for c in edited],
                ["", "Новая встреча", "2026-12-24", "none", "0", "", ""],
                ["", "Плохая дата", "31.02.2025", "", "", "", ""],
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
    assert rows[0][:3] == [str(existing.id), "Изменено", "31.02.2025"]  # kept as typed
    assert rows[1][:2] == [stored["Новая встреча"].id, "Новая встреча"]  # got its ID
    assert rows[2][:3] == ["", "Плохая дата", "31.02.2025"]  # kept as typed
