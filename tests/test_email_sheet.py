"""The Email sheet (allowed Gmail senders) and the Inbox sheet.

An address must reach the database exactly as the sender really writes it: the
bot reads only messages from these addresses, so a wrongly accepted address
either lets a stranger in or silently drops real letters. Every row is checked
strictly, confirmed on the ID cell (green + note) or explained (red + note).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime

import pytest

from app import db, senders
from app.gmail.client import MessageList
from app.gmail.importer import build_query, import_messages
from app.google_sheets.feedback import GREEN, RED
from app.google_sheets.mappers import INBOX_HEADERS, SENDER_HEADERS
from app.inbox import create_message
from app.models.allowed_sender import AllowedSender
from tests.test_db import _alembic_config, _sqlite_url
from tests.test_gmail_importer import FakeGmailClient, _gmail_message
from tests.test_google_sheets_two_way import Kind, SheetStore, _records, _sync, trim
from tests.test_sheet_ux import _sync_ignoring_row_errors

SENDERS = Kind(
    sheet="Email", headers=SENDER_HEADERS, model=AllowedSender, new_row=[], invalid_new_row=[],
    name_col=1, bad_col=1, bad_value="", importer=None,
)
_PENDING = re.compile(r"^new-[0-9a-f]{12}$")


def _store(*rows: list[object]) -> SheetStore:
    return SheetStore({"Email": [list(SENDER_HEADERS), *[list(r) for r in rows]]})


def _emails() -> list[str]:
    return [s.email for s in _records(SENDERS)]


# --- what counts as an address -----------------------------------------------------------


@pytest.mark.parametrize(
    "typed, stored",
    [
        ("support@liteserver.nl", "support@liteserver.nl"),
        ("  Admin@ZTV.su  ", "admin@ztv.su"),
        ("mailto:billing@example.com", "billing@example.com"),
        ("Поддержка <help@example.org>", "help@example.org"),
        ("first.last+tag@sub.example.co.uk", "first.last+tag@sub.example.co.uk"),
    ],
)
def test_addresses_are_normalized_exactly(typed: str, stored: str) -> None:
    assert senders.normalize_sender(typed) == stored


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "   ",
        "example.com",  # a bare domain is not an address
        "@example.com",  # nor is "everything from this domain"
        "*@example.com",
        "name@",
        "name@example",  # no dotted domain
        "name@example..com",
        "two words@example.com",
        "a@example.com, b@example.com",  # one address per row
        "a@example.com; b@example.com",
        "a@example.com b@example.com",
        "a@b.com) OR (in:anywhere",  # nothing may change the Gmail search
        "a@b.com OR from:c@d.com",
        'a@b.com"',
        "http://example.com",
        "имя@пример.рф",  # not supported: matching here is ASCII only
    ],
)
def test_bad_addresses_are_rejected(typed: str) -> None:
    with pytest.raises(ValueError):
        senders.normalize_sender(typed)


# --- the sheet: new rows --------------------------------------------------------------------


def test_a_new_address_is_created_and_confirmed_green(schema: None) -> None:
    store = _store(["", "Billing@Example.com", ""])
    _sync(store)

    [sender] = _records(SENDERS)
    assert (sender.email, sender.is_active) == ("billing@example.com", True)
    assert _PENDING.match(sender.sheet_key)
    assert trim(store.sheets["Email"][1]) == [sender.id, "billing@example.com", "да"]  # canonical form
    assert store.colour("Email", 2) == GREEN
    note = store.note("Email", 2)
    assert "✓ Принято" in note and "billing@example.com" in note


def test_an_empty_active_cell_means_yes(schema: None) -> None:
    _sync(_store(["", "a@example.com", ""]))
    assert _records(SENDERS)[0].is_active is True


def test_repeated_syncs_change_nothing(schema: None) -> None:
    store = _store(["", "a@example.com", "да"], ["", "b@example.com", "нет"])
    for _ in range(3):
        _sync(store)
    assert _emails() == ["a@example.com", "b@example.com"]
    assert [s.is_active for s in _records(SENDERS)] == [True, False]
    assert len(store.data_rows("Email")) == 2


def test_a_checkbox_stays_a_checkbox(schema: None) -> None:
    store = _store(["", "a@example.com", True])
    _sync(store)
    assert store.sheets["Email"][1][2] is True
    store.sheets["Email"][1][2] = False
    _sync(store)
    assert _records(SENDERS)[0].is_active is False and store.sheets["Email"][1][2] is False


# --- bad rows: explained in red, nothing is saved, the row is left as typed -----------------------


@pytest.mark.parametrize(
    "typed", ["example.com", "@example.com", "a@example.com, b@example.com", "not an address", "a@b"]
)
def test_a_bad_address_is_red_with_a_reason_and_saves_nothing(schema: None, typed: str) -> None:
    store = _store(["", typed, ""], ["", "good@example.com", ""])
    _sync_ignoring_row_errors(store)

    assert _emails() == ["good@example.com"]  # the good neighbour is still imported
    assert store.colour("Email", 2) == RED
    assert "✗ Строка не принята" in store.note("Email", 2)
    assert store.sheets["Email"][1][:2] == ["", typed]  # exactly as typed
    assert store.colour("Email", 3) == GREEN
    _sync_ignoring_row_errors(store)  # stays red until it is fixed; nothing changes
    assert _emails() == ["good@example.com"]


def test_a_fixed_address_turns_green(schema: None) -> None:
    store = _store(["", "example.com", ""])
    _sync_ignoring_row_errors(store)
    assert store.colour("Email", 2) == RED
    store.sheets["Email"][1][1] = "info@example.com"
    _sync(store)
    assert _emails() == ["info@example.com"] and store.colour("Email", 2) == GREEN


def test_the_same_address_twice_is_accepted_once(schema: None) -> None:
    store = _store(["", "a@example.com", ""], ["", "A@Example.com", ""])
    _sync_ignoring_row_errors(store)
    assert _emails() == ["a@example.com"]
    assert store.colour("Email", 2) == GREEN and store.colour("Email", 3) == RED
    assert "already in the list" in store.note("Email", 3)


def test_an_unknown_id_is_rejected(schema: None) -> None:
    store = _store(["999", "a@example.com", ""])
    _sync_ignoring_row_errors(store)
    assert _emails() == [] and store.colour("Email", 2) == RED


# --- editing ------------------------------------------------------------------------------------------


def test_editing_an_address_updates_the_same_record(schema: None) -> None:
    store = _store(["", "old@example.com", ""])
    _sync(store)
    sender_id = _records(SENDERS)[0].id
    store.sheets["Email"][1][1] = "New@Example.com"
    _sync(store)
    [sender] = _records(SENDERS)
    assert (sender.id, sender.email) == (sender_id, "new@example.com")
    assert store.sheets["Email"][1][1] == "new@example.com"


def test_changing_an_address_to_another_existing_one_is_rejected(schema: None) -> None:
    store = _store(["", "a@example.com", ""], ["", "b@example.com", ""])
    _sync(store)
    store.sheets["Email"][2][1] = "a@example.com"
    _sync_ignoring_row_errors(store)
    assert sorted(_emails()) == ["a@example.com", "b@example.com"]
    assert store.colour("Email", 3) == RED
    assert store.sheets["Email"][2][1] == "a@example.com"  # left as typed


def test_an_empty_address_cell_of_a_known_row_means_no_change(schema: None) -> None:
    store = _store(["", "a@example.com", "да"])
    _sync(store)
    store.sheets["Email"][1][1] = ""
    _sync(store)
    assert _emails() == ["a@example.com"]


def test_active_no_pauses_the_sender(schema: None) -> None:
    store = _store(["", "a@example.com", ""])
    _sync(store)
    store.sheets["Email"][1][2] = "нет"
    _sync(store)

    async def active() -> frozenset[str]:
        async with db.get_session() as session:
            return await senders.active_emails(session)

    assert asyncio.run(active()) == frozenset()
    assert "на паузе" in store.note("Email", 2)


def test_the_delete_word_removes_the_address(schema: None) -> None:
    store = _store(["", "a@example.com", ""], ["", "b@example.com", ""])
    _sync(store)
    store.sheets["Email"][1][2] = "УДАЛИТЬ"
    _sync(store)
    assert _emails() == ["b@example.com"]
    assert store.colour("Email", 2) is None


def test_clearing_a_row_does_not_delete_the_address(schema: None) -> None:
    store = _store(["", "a@example.com", ""])
    _sync(store)
    store.sheets["Email"][1] = ["", "", ""]
    _sync(store)
    assert _emails() == ["a@example.com"]
    assert store.sheets["Email"][1][1] == "a@example.com"  # the row comes back


def test_notes_right_of_the_table_are_kept(schema: None) -> None:
    store = _store(["", "a@example.com", "", "это мой банк"])
    _sync(store)
    assert store.sheets["Email"][1][3] == "это мой банк"


# --- the Gmail import follows the list ------------------------------------------------------------------


def _import(client: FakeGmailClient):
    async def run():
        async with db.get_session() as session:
            return await import_messages(session, client)

    return asyncio.run(run())


def test_an_address_added_in_the_sheet_is_read_by_the_import(schema: None) -> None:
    _sync(_store(["", "billing@example.com", ""]))
    client = FakeGmailClient(
        pages=[MessageList(["m1", "m2"], None)],
        messages={
            "m1": _gmail_message("m1", "Billing@Example.com", subject="Счёт"),
            "m2": _gmail_message("m2", "stranger@example.com", subject="Спам"),
        },
    )
    stats = _import(client)
    assert (stats.imported, stats.skipped_not_allowed) == (1, 1)
    assert client.queries == ["from:(billing@example.com)"]


def test_a_paused_address_is_not_read(schema: None) -> None:
    _sync(_store(["", "a@example.com", ""], ["", "b@example.com", "нет"]))
    client = FakeGmailClient(pages=[MessageList([], None)])
    _import(client)
    assert client.queries == ["from:(a@example.com)"]


def test_without_active_senders_nothing_is_fetched(schema: None) -> None:
    _sync(_store(["", "a@example.com", "нет"]))
    client = FakeGmailClient(
        pages=[MessageList(["m1"], None)], messages={"m1": _gmail_message("m1", "a@example.com")}
    )
    stats = _import(client)
    assert client.queries == [] and stats.found == 0  # the mailbox is not even listed


def test_the_query_lists_every_active_sender_sorted() -> None:
    assert build_query({"b@x.com", "a@x.com"}) == "from:(a@x.com OR b@x.com)"


# --- the migration keeps the two old addresses -----------------------------------------------------------


def test_the_migration_seeds_the_two_original_addresses(tmp_path) -> None:
    from alembic import command

    url = _sqlite_url(tmp_path)
    command.upgrade(_alembic_config(url), "head")
    path = url.split("///", 1)[1]
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT email, is_active FROM allowed_senders ORDER BY id").fetchall()
    assert rows == [("support@liteserver.nl", 1), ("admin@ztv.su", 1)]


# --- Inbox (export only) -----------------------------------------------------------------------------------


def test_the_inbox_sheet_shows_received_letters_as_plain_text(schema: None) -> None:
    async def add() -> None:
        async with db.get_session() as session:
            await create_message(
                session, source="gmail", external_id="x1", sender="a@example.com",
                subject='=HYPERLINK("http://evil","click")', body="b", received_at=datetime(2999, 1, 2, 9, 30),
            )
            await session.commit()

    asyncio.run(add())
    store = SheetStore()
    _sync(store)
    assert [str(c) for c in store.sheets["Inbox"][0]] == INBOX_HEADERS
    row = store.sheets["Inbox"][1]
    assert row[1] == "02.01.2999 12:30"  # Moscow time
    assert row[3] == '=HYPERLINK("http://evil","click")'  # text, never a formula
    assert row[8] == "ждёт разбора"


def test_the_inbox_sheet_is_rewritten_not_edited(schema: None) -> None:
    store = SheetStore()
    _sync(store)
    store.sheets["Inbox"].append(["junk", "typed by the user"])
    _sync(store)
    assert all("junk" not in str(c) for r in store.sheets["Inbox"][1:] for c in r)


# --- the Settings tab is gone: deleting it breaks nothing --------------------------------------------------------


def test_deleting_the_settings_tab_breaks_nothing_and_it_is_not_recreated(schema: None) -> None:
    store = SheetStore({"Email": [list(SENDER_HEADERS)], "Settings": [["Параметр", "Значение"], ["x", "1"]]})
    _sync(store)
    store.roles[store.sheet_id("Settings")] = "settings"  # an older version marked it
    _sync(store)
    del store.sheets["Settings"]  # the user deletes the tab
    del store._ids["Settings"]
    _sync(store)
    _sync(store)
    assert "Settings" not in store.sheets
    assert {"Events", "Expenses", "Reminders", "Inbox", "Email"} <= set(store.sheets)


def test_a_letter_subject_that_looks_like_a_formula_is_sent_as_text() -> None:
    from app.google_sheets.mappers import inbox_to_row
    from app.models.inbox import InboxMessage, InboxStatus

    message = InboxMessage(
        id=1, source="gmail", external_id="x", sender="a@example.com",
        subject='=HYPERLINK("http://evil","click")', status=InboxStatus.NEW,
    )
    assert inbox_to_row(message)[3].startswith("'=")  # the apostrophe keeps Sheets from evaluating it
