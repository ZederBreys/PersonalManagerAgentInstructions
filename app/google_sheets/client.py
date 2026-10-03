"""Minimal async transport client for the Google Sheets API.

The official ``google-api-python-client`` is synchronous, so every API call is
offloaded to a worker thread via :func:`asyncio.to_thread` to keep the asyncio
event loop (and therefore the scheduler) responsive.

Security: the service-account credentials and spreadsheet id are never logged;
API errors are reported by message and HTTP status only, never by URL or
credentials content.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import Settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# Write values as-is (no Sheets auto-typing), so round-trips stay predictable:
# our mappers are the only thing that interprets dates, booleans and money.
_VALUE_INPUT_OPTION = "RAW"
# Read values back as strings (the sheet's display format). This keeps the
# mapping layer in control: dates stay ISO text, amounts stay "12.50" strings,
# booleans stay "да"/"нет" text, so the mappers never have to guess a type.
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

    def _get_service(self) -> Any:
        if self._service is None:
            self._service = _build_service(self._service_account_file)
        return self._service

    async def get_values(self, range_name: str) -> list[list[object]]:
        """Read a range; return the list of rows (no header interpretation)."""

        return await asyncio.to_thread(self._get_values, range_name)

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

        await asyncio.to_thread(self._update_values, range_name, values)

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

        await asyncio.to_thread(self._append_values, range_name, values)

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

        await asyncio.to_thread(self._clear, range_name)

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

        return await asyncio.to_thread(self._get_sheet_titles)

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

        await asyncio.to_thread(self._add_sheet, title)

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
