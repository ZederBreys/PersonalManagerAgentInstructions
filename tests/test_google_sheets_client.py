"""Tests for the Google Sheets transport client (no real Google API)."""

from __future__ import annotations

import asyncio
import json
import threading

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.google_sheets.client import GoogleSheetsClient, GoogleSheetsError


def _http_error(status: int, message: str) -> HttpError:
    resp = httplib2.Response({"status": str(status)})
    content = json.dumps({"error": {"message": message}}).encode()
    return HttpError(resp, content)


class _Request:
    def __init__(self, owner, result):
        self._owner = owner
        self._result = result

    def execute(self):
        self._owner.thread_ids.append(threading.get_ident())
        if self._owner.error is not None:
            raise self._owner.error
        return self._result


class _FakeValues:
    def __init__(self):
        self.calls = []
        self.result = {"values": []}
        self.error = None
        self.thread_ids = []

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return _Request(self, self.result)

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return _Request(self, {})

    def append(self, **kwargs):
        self.calls.append(("append", kwargs))
        return _Request(self, {})

    def clear(self, **kwargs):
        self.calls.append(("clear", kwargs))
        return _Request(self, {})


class _FakeSpreadsheets:
    def __init__(self):
        self.values_obj = _FakeValues()
        self.get_calls = []
        self.get_result = {"sheets": [{"properties": {"title": "Events"}}]}
        self.batch_calls = []
        self.error = None
        self.thread_ids = []

    def values(self):
        return self.values_obj

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _Request(self, self.get_result)

    def batchUpdate(self, **kwargs):
        self.batch_calls.append(kwargs)
        return _Request(self, {})


class _FakeService:
    def __init__(self):
        self.spreadsheets_obj = _FakeSpreadsheets()

    def spreadsheets(self):
        return self.spreadsheets_obj


def _client(service):
    return GoogleSheetsClient("creds.json", "spreadsheet-id", service=service)


def test_get_values_returns_rows():
    service = _FakeService()
    service.spreadsheets_obj.values_obj.result = {"values": [["a", "b"], ["c", "d"]]}
    client = _client(service)

    result = asyncio.run(client.get_values("Events!A1:B2"))

    assert result == [["a", "b"], ["c", "d"]]
    method, kwargs = service.spreadsheets_obj.values_obj.calls[0]
    assert method == "get"
    assert kwargs["spreadsheetId"] == "spreadsheet-id"
    assert kwargs["range"] == "'Events'!A1:B2"  # the tab title is always quoted
    assert kwargs["valueRenderOption"] == "FORMATTED_VALUE"


def test_update_values_calls_update():
    service = _FakeService()
    client = _client(service)

    asyncio.run(client.update_values("Events!A1", [["ID", "Событие"]]))

    method, kwargs = service.spreadsheets_obj.values_obj.calls[0]
    assert method == "update"
    assert kwargs["spreadsheetId"] == "spreadsheet-id"
    assert kwargs["range"] == "'Events'!A1"
    assert kwargs["valueInputOption"] == "USER_ENTERED"
    assert kwargs["body"] == {"values": [["ID", "Событие"]]}


def test_append_values_calls_append():
    service = _FakeService()
    client = _client(service)

    asyncio.run(client.append_values("Events", [["1", "x"]]))

    method, kwargs = service.spreadsheets_obj.values_obj.calls[0]
    assert method == "append"
    assert kwargs["insertDataOption"] == "INSERT_ROWS"
    assert kwargs["body"] == {"values": [["1", "x"]]}


def test_clear_calls_clear():
    service = _FakeService()
    client = _client(service)

    asyncio.run(client.clear("Events"))

    method, kwargs = service.spreadsheets_obj.values_obj.calls[0]
    assert method == "clear"
    assert kwargs["range"] == "'Events'"


def test_get_values_raises_google_sheets_error_on_http_error():
    service = _FakeService()
    service.spreadsheets_obj.values_obj.error = _http_error(500, "boom")
    client = _client(service)

    with pytest.raises(GoogleSheetsError) as excinfo:
        asyncio.run(client.get_values("Events"))

    assert excinfo.value.http_status == 500
    assert "boom" in str(excinfo.value)


def test_error_message_does_not_leak_config():
    service = _FakeService()
    service.spreadsheets_obj.values_obj.error = _http_error(403, "forbidden")
    client = _client(service)

    with pytest.raises(GoogleSheetsError) as excinfo:
        asyncio.run(client.get_values("Events"))

    text = str(excinfo.value)
    assert "creds.json" not in text
    assert "spreadsheet-id" not in text


def test_missing_config_raises():
    with pytest.raises(ValueError):
        GoogleSheetsClient("", "spreadsheet-id")
    with pytest.raises(ValueError):
        GoogleSheetsClient("creds.json", "")


def test_get_values_offloads_to_thread():
    service = _FakeService()
    service.spreadsheets_obj.values_obj.result = {"values": [["x"]]}
    client = _client(service)
    main_thread = threading.get_ident()

    asyncio.run(client.get_values("Events"))

    assert service.spreadsheets_obj.values_obj.thread_ids
    assert service.spreadsheets_obj.values_obj.thread_ids[0] != main_thread
