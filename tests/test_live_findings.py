"""Regression tests for what the live run against a copy of the real spreadsheet found.

Each test reproduces a situation the real Google Sheets produced: custom date
formats, amounts typed as dates, notes beside deleted rows, checkboxes, and edits
made while a sync is running.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.config import Settings
from app.google_sheets.client import GoogleSheetsClient
from app.google_sheets.feedback import GREEN, RED
from app.jobs import POLL_PAUSE_AFTER_ERROR, POLL_PAUSE_BAD_RANGE, Services, SheetsSyncError, sheets_poll, sheets_sync
from tests.test_google_sheets_two_way import EVENTS, EXPENSES, SheetStore, _records, _store, _sync
from tests.test_sheet_ux import (  # noqa: F401 - clock is a fixture used below
    EVENT_ROW,
    EXPENSE_ROW,
    _Clock,
    _count,
    _run,
    _services,
    _sync_ignoring_row_errors,
    clock,
)

SERIAL = (date(2999, 8, 24) - date(1899, 12, 30)).days  # how Sheets stores 24.08.2999


# --- notes the user keeps beside rows ---------------------------------------------------


def test_a_row_holding_only_a_note_is_not_a_record(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW, ["", "", "", "", "", "", "", "", "", "просто заметка"])
    _sync(store)  # must not raise: nothing is wrong
    assert [e.name for e in _records(EVENTS)] == ["Годовщина"]
    assert store.colour("Events", 3) is None and store.note("Events", 3) is None  # not flagged


def test_the_note_left_after_a_deletion_does_not_turn_the_row_red(schema: None) -> None:
    first = list(EVENT_ROW[:7]) + ["", "", "заметка к первому"]
    second = ["", "Второе", "25.08.2999", "none", "0", "", ""] + ["", "", "заметка ко второму"]
    store = _store(EVENTS, first, second)
    _sync(store)
    store.sheets["Events"][1][6] = "удалить"
    _sync(store)  # the cleared row still holds its note in column J
    assert [e.name for e in _records(EVENTS)] == ["Второе"]
    assert store.sheets["Events"][1][9] == "заметка к первому"  # the user's note is kept
    assert store.colour("Events", 2) is None  # and the row is not an error
    _sync(store)  # nothing changes on the next run either


# --- displayed text is not the value --------------------------------------------------------


def test_a_custom_date_format_does_not_break_the_import(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    store.sheets["Events"][1][2] = SERIAL  # the real value: a date serial ...
    store.display[("Events", 1, 2)] = "суббота, 24 августа 2999"  # ... shown in the user's format
    store.sheets["Events"][1][1] = "Новое имя"

    _sync(store)  # must not raise

    [event] = _records(EVENTS)
    assert (event.name, event.next_date) == ("Новое имя", date(2999, 8, 24))
    assert store.colour("Events", 2) == GREEN


def test_a_row_that_is_not_accepted_is_never_rewritten(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    store.sheets["Events"][1][2] = SERIAL
    store.display[("Events", 1, 2)] = "суббота, 24 августа 2999"
    store.sheets["Events"][1][3] = "1"  # a mistake: the row is rejected

    _sync_ignoring_row_errors(store)

    assert store.colour("Events", 2) == RED
    assert store.sheets["Events"][1][2] == SERIAL  # the date is still a date, not its text


def test_an_amount_whose_format_hides_its_value_is_rejected_not_guessed(schema: None) -> None:
    store = _store(EXPENSES, EXPENSE_ROW)
    _sync(store)
    amount = _records(EXPENSES)[0].amount_minor
    store.sheets["Expenses"][1][2] = 12.5  # what the bot wrote ...
    store.display[("Expenses", 1, 2)] = "11.1"  # ... shown through a leftover DATE format

    _sync_ignoring_row_errors(store)
    _sync_ignoring_row_errors(store)  # a second run must not move the amount either

    assert _records(EXPENSES)[0].amount_minor == amount  # no 12.50 -> 11.10 -> ... drift
    assert store.colour("Expenses", 2) == RED
    assert "holds the number 12.5" in store.note("Expenses", 2)
    assert store.sheets["Expenses"][1][2] == 12.5  # untouched


def test_an_amount_typed_like_a_date_is_not_taken_for_a_sum(schema: None) -> None:
    row = list(EXPENSE_ROW)
    store = _store(EXPENSES, row)
    store.sheets["Expenses"][1][2] = 46154  # Sheets read "12.5" as 12 May: a date serial
    store.display[("Expenses", 1, 2)] = "12.5"

    _sync_ignoring_row_errors(store)

    assert _records(EXPENSES) == []  # not created with the amount 46154.00
    assert store.colour("Expenses", 2) == RED


def test_a_currency_formatted_amount_is_accepted(schema: None) -> None:
    store = _store(EXPENSES, EXPENSE_ROW)
    _sync(store)
    store.sheets["Expenses"][1][2] = 2500.5
    store.display[("Expenses", 1, 2)] = "2 500,50 ₽"
    store.sheets["Expenses"][1][1] = "Переименован"

    _sync(store)

    [expense] = _records(EXPENSES)
    assert (expense.name, expense.amount_minor) == ("Переименован", 250050)


# --- checkboxes ---------------------------------------------------------------------------------


def test_a_checkbox_in_the_active_column_stays_a_checkbox(schema: None) -> None:
    row = list(EVENT_ROW)
    row[6] = True  # a ticked checkbox
    store = _store(EVENTS, row)
    _sync(store)
    assert _records(EVENTS)[0].is_active is True
    assert store.sheets["Events"][1][6] is True  # not overwritten by the word "да"

    store.sheets["Events"][1][6] = False  # the user unticks it
    _sync(store)
    assert _records(EVENTS)[0].is_active is False
    assert store.sheets["Events"][1][6] is False


def test_a_text_cell_in_the_active_column_still_gets_words(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    assert store.sheets["Events"][1][6] == "да"


# --- the API skips null values; rows that are not ours are all null ----------------------------------


def test_untouched_rows_are_sent_as_nulls(schema: None) -> None:
    store = _store(EVENTS, ["", "Плохая", "24.08.2999", "1", "", "", ""], EVENT_ROW)
    _sync_ignoring_row_errors(store)
    # the first row is the user's mistake: it must be exactly as typed
    assert store.sheets["Events"][1][:4] == ["", "Плохая", "24.08.2999", "1"]


# --- edits while a sync is running ---------------------------------------------------------------------


def test_an_edit_made_between_import_and_export_is_not_overwritten(schema: None, clock: _Clock) -> None:
    store = _store(EVENTS, EVENT_ROW)
    services = _services(store)
    requested: list[int] = []
    services.request_sync = lambda: requested.append(1)
    for _ in range(3):  # reach a calm baseline
        _run(sheets_sync(services))
        if services.poll_applied is not None:
            break
    assert services.poll_applied is not None

    reads = {"n": 0}

    def user_types_during_the_sync(s: SheetStore, range_name: str) -> None:
        if range_name == "Events":
            reads["n"] += 1
            if reads["n"] == 3:  # the export's read: the import read the sheet just before
                s.sheets["Events"][1][1] = "Правка во время синхронизации"

    store.on_read = user_types_during_the_sync
    _run(sheets_sync(services))
    store.on_read = None

    assert store.sheets["Events"][1][1] == "Правка во время синхронизации"  # not overwritten
    assert _records(EVENTS)[0].name == "Годовщина"  # the database has not seen it yet
    assert services.poll_applied is None  # so the sheet is not marked as handled

    clock.now += 1
    for _ in range(2):
        _run(sheets_poll(services))
    assert requested == [1]  # the watcher asks for another pass ...
    _run(sheets_sync(services))
    assert _records(EVENTS)[0].name == "Правка во время синхронизации"  # ... which picks the edit up


def test_a_quiet_sync_claims_the_baseline(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    services = _services(store)
    for _ in range(3):
        _run(sheets_sync(services))
    assert services.poll_applied is not None and services.poll_seen == services.poll_applied


# --- the watcher's backoff ---------------------------------------------------------------------------------


def test_a_renamed_tab_is_retried_soon_but_other_errors_back_off_longer(schema: None, clock: _Clock) -> None:
    store = _store(EVENTS, EVENT_ROW)
    services = _services(store)
    for _ in range(3):
        _run(sheets_sync(services))
    store.rename("Events", "Напоминания")
    _run(sheets_poll(services))  # HTTP 400: the cached title is stale
    assert services.poll_pause_until - clock.now == POLL_PAUSE_BAD_RANGE < POLL_PAUSE_AFTER_ERROR


# --- the raw read option ----------------------------------------------------------------------------------------


class _RecordingService:
    def __init__(self) -> None:
        self.render_options: list[str] = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kwargs):
        self.render_options.append(kwargs["valueRenderOption"])
        return self

    def execute(self):
        return {"values": [[1]]}


def test_raw_reads_use_the_unformatted_render_option() -> None:
    service = _RecordingService()
    client = GoogleSheetsClient("unused.json", "id", service=service)
    _run(client.get_values("Events"))
    _run(client.get_values("Events", raw=True))
    assert service.render_options == ["FORMATTED_VALUE", "UNFORMATTED_VALUE"]
