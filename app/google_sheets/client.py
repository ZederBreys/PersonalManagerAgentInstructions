"""Minimal async transport client for the Google Sheets API.

The official ``google-api-python-client`` is synchronous, so every API call is
offloaded to a worker thread via :func:`asyncio.to_thread` to keep the asyncio
event loop (and therefore the scheduler) responsive.

Thread safety: the underlying ``httplib2.Http`` (one TLS connection) is not
thread-safe. Two worker threads using it at once make OpenSSL read the same
connection concurrently, which corrupts native memory (``free(): corrupted
unsorted chunks``, segfaults, hangs). Every call of a client instance is
therefore serialized with a lock, so jobs may share one client safely.

Security: the service-account credentials and spreadsheet id are never logged;
API errors are reported by message and HTTP status only, never by URL or
credentials content.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from app.config import Settings

_T = TypeVar("_T")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# Write values as if the user typed them. With RAW, every value we wrote back
# (including the user's own cells, which are read as display strings) became
# plain text: dates and numbers turned into text and Sheets showed a leading
# apostrophe on them. Our exports send real numbers for numbers and ISO dates
# (which Sheets turns into date cells); free text that could be misread as a
# number, date or formula is protected explicitly (see mappers.text_cell).
_VALUE_INPUT_OPTION = "USER_ENTERED"
# Read values back as strings (the sheet's display format), whatever the cell
# type: a date cell comes back as "24.08.2025" in a ru_RU sheet, a number as
# "12,5". The mappers accept those display forms.
_VALUE_RENDER_OPTION = "FORMATTED_VALUE"


class GoogleSheetsError(Exception):
    """Raised when a Google Sheets API call fails.

    ``message`` is the Google-provided error reason; ``http_status`` is the HTTP
    status code when a response was received. The original exception is always
    preserved in ``__cause__``.
    """

    def __init__(self, *, message: str = "Google Sheets API error", http_status: int | None = None) -> None:
        self.message = message
        self.http_status = http_status
        super().__init__(message)


def _http_status(exc: Any) -> int | None:
    return getattr(exc.resp, "status", None)


def _http_message(exc: Any) -> str:
    try:
        reason = exc._get_reason()  # noqa: SLF001 - only way to get the reason
    except Exception:  # pragma: no cover - defensive fallback
        reason = None
    return reason or str(exc)


def _build_service(service_account_file: str) -> Any:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_file(
        service_account_file, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=credentials)


class GoogleSheetsClient:
    """Async wrapper around the synchronous Google Sheets API client.

    A single underlying service/client is built lazily (on first use) and reused.
    For tests a fake ``service`` may be injected, which skips credential loading
    entirely.
    """

    def __init__(
        self,
        service_account_file: str,
        spreadsheet_id: str,
        *,
        service: Any | None = None,
    ) -> None:
        if not service_account_file:
            raise ValueError("google_service_account_file is not configured")
        if not spreadsheet_id:
            raise ValueError("google_spreadsheet_id is not configured")

        self._service_account_file = service_account_file
        self._spreadsheet_id = spreadsheet_id
        self._service = service
        # Serializes service creation and every request (see module docstring).
        self._lock = threading.Lock()

    async def _call(self, func: Callable[..., _T], *args: Any) -> _T:
        """Run a blocking API call in a worker thread, one call at a time."""

        def locked() -> _T:
            with self._lock:
                return func(*args)

        return await asyncio.to_thread(locked)

    def _get_service(self) -> Any:
        if self._service is None:
            self._service = _build_service(self._service_account_file)
        return self._service

    async def get_values(self, range_name: str) -> list[list[object]]:
        """Read a range; return the list of rows (no header interpretation)."""

        return await self._call(self._get_values, range_name)

    def _get_values(self, range_name: str) -> list[list[object]]:
        from googleapiclient.errors import HttpError

        try:
            result = (
                self._get_service()
                .spreadsheets()
                .values()
                .get(
                    spreadsheetId=self._spreadsheet_id,
                    range=range_name,
                    valueRenderOption=_VALUE_RENDER_OPTION,
                )
                .execute()
            )
        except HttpError as exc:
            raise GoogleSheetsError(
                message=_http_message(exc), http_status=_http_status(exc)
            ) from exc
        return result.get("values", [])

    async def update_values(self, range_name: str, values: list[list[object]]) -> None:
        """Overwrite a range with ``values``."""

        await self._call(self._update_values, range_name, values)

    def _update_values(self, range_name: str, values: list[list[object]]) -> None:
        from googleapiclient.errors import HttpError

        try:
            self._get_service().spreadsheets().values().update(
                spreadsheetId=self._spreadsheet_id,
                range=range_name,
                valueInputOption=_VALUE_INPUT_OPTION,
                body={"values": values},
            ).execute()
        except HttpError as exc:
            raise GoogleSheetsError(
                message=_http_message(exc), http_status=_http_status(exc)
            ) from exc

    async def append_values(self, range_name: str, values: list[list[object]]) -> None:
        """Append rows after the existing content of ``range_name``."""

        await self._call(self._append_values, range_name, values)

    def _append_values(self, range_name: str, values: list[list[object]]) -> None:
        from googleapiclient.errors import HttpError

        try:
            self._get_service().spreadsheets().values().append(
                spreadsheetId=self._spreadsheet_id,
                range=range_name,
                valueInputOption=_VALUE_INPUT_OPTION,
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            ).execute()
        except HttpError as exc:
            raise GoogleSheetsError(
                message=_http_message(exc), http_status=_http_status(exc)
            ) from exc

    async def clear(self, range_name: str) -> None:
        """Clear the contents of ``range_name``."""

        await self._call(self._clear, range_name)

    def _clear(self, range_name: str) -> None:
        from googleapiclient.errors import HttpError

        try:
            self._get_service().spreadsheets().values().clear(
                spreadsheetId=self._spreadsheet_id, range=range_name, body={}
            ).execute()
        except HttpError as exc:
            raise GoogleSheetsError(
                message=_http_message(exc), http_status=_http_status(exc)
            ) from exc

    async def get_sheet_titles(self) -> list[str]:
        """Return the titles of the existing sheets, in spreadsheet order."""

        return await self._call(self._get_sheet_titles)

    def _get_sheet_titles(self) -> list[str]:
        from googleapiclient.errors import HttpError

        try:
            result = (
                self._get_service()
                .spreadsheets()
                .get(spreadsheetId=self._spreadsheet_id, fields="sheets.properties.title")
                .execute()
            )
        except HttpError as exc:
            raise GoogleSheetsError(
                message=_http_message(exc), http_status=_http_status(exc)
            ) from exc
        return [
            sheet["properties"]["title"]
            for sheet in result.get("sheets", [])
        ]

    async def add_sheet(self, title: str) -> None:
        """Create a new sheet with the given ``title`` (idempotent by caller)."""

        await self._call(self._add_sheet, title)

    def _add_sheet(self, title: str) -> None:
        from googleapiclient.errors import HttpError

        body = {"requests": [{"addSheet": {"properties": {"title": title}}}]}
        try:
            self._get_service().spreadsheets().batchUpdate(
                spreadsheetId=self._spreadsheet_id, body=body
            ).execute()
        except HttpError as exc:
            raise GoogleSheetsError(
                message=_http_message(exc), http_status=_http_status(exc)
            ) from exc


def create_client_from_settings(settings: Settings) -> GoogleSheetsClient | None:
    """Build a client when Google Sheets is configured, else return ``None``.

    When credentials or spreadsheet id are missing the application must keep
    working and simply skip all Google Sheets work.
    """

    if not settings.google_sheets_enabled:
        return None
    return GoogleSheetsClient(
        settings.google_service_account_file, settings.google_spreadsheet_id
    )
