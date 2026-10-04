"""Tests for the Gmail importer (fake Gmail client, real temp SQLite)."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime

import pytest
from sqlalchemy import select

from app import db
from app.gmail.client import GmailError, MessageList
from app.gmail.importer import ImportStats, import_messages
from app.models.allowed_sender import AllowedSender
from app.models.inbox import InboxMessage, InboxStatus


@pytest.fixture(autouse=True)
def allowed_senders(schema: None) -> None:
    """The two addresses every installation starts with (the migration inserts them)."""

    async def seed() -> None:
        async with db.get_session() as session:
            session.add_all([AllowedSender(email="support@liteserver.nl"), AllowedSender(email="admin@ztv.su")])
            await session.commit()

    asyncio.run(seed())


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _gmail_message(
    message_id: str,
    sender: str,
    subject: str = "Test",
    body_text: str = "Hello",
    internal_date: str = "1609459200000",
) -> dict:
    return {
        "id": message_id,
        "internalDate": internal_date,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": f"Name <{sender}>"},
                {"name": "Subject", "value": subject},
            ],
            "body": {"data": _b64url(body_text)},
        },
    }


class FakeGmailClient:
    def __init__(self, *, pages=None, messages=None, errors=None) -> None:
        self.pages = list(pages) if pages is not None else []
        self.messages = messages or {}
        self.errors = errors or {}
        self._page_index = 0
        self.queries: list[str | None] = []

    async def list_messages(self, *, q=None, max_results=None, page_token=None):
        self.queries.append(q)
        if self._page_index >= len(self.pages):
            return MessageList([], None)
        page = self.pages[self._page_index]
        self._page_index += 1
        ids = page.messages
        if max_results is not None:
            ids = ids[:max_results]
        return MessageList(ids, page.next_page_token)

    async def get_message(self, message_id):
        if message_id in self.errors:
            raise self.errors[message_id]
        return self.messages[message_id]


async def _rows() -> list[InboxMessage]:
    async with db.get_session() as session:
        result = await session.execute(select(InboxMessage).order_by(InboxMessage.id))
        return list(result.scalars().all())


def test_import_allowed_message(schema: None) -> None:
    client = FakeGmailClient(
        pages=[MessageList(["m1"], None)],
        messages={"m1": _gmail_message("m1", "support@liteserver.nl")},
    )

    async def _run() -> None:
        async with db.get_session() as session:
            stats = await import_messages(session, client)
        assert (stats.found, stats.imported) == (1, 1)
        rows = await _rows()
        assert len(rows) == 1
        row = rows[0]
        assert row.source == "gmail"
        assert row.external_id == "m1"
        assert row.sender == "support@liteserver.nl"
        assert row.subject == "Test"
        assert row.body == "Hello"
        assert row.received_at == datetime(2021, 1, 1)
        assert row.status is InboxStatus.NEW
        assert row.classification is None

    asyncio.run(_run())


def test_import_forbidden_sender_skipped(schema: None) -> None:
    client = FakeGmailClient(
        pages=[MessageList(["m1"], None)],
        messages={"m1": _gmail_message("m1", "evil@example.com")},
    )

    async def _run() -> None:
        async with db.get_session() as session:
            stats = await import_messages(session, client)
        assert stats.skipped_not_allowed == 1
        assert stats.imported == 0
        assert await _rows() == []

    asyncio.run(_run())


def test_import_duplicate_second_run(schema: None) -> None:
    client = FakeGmailClient(
        pages=[MessageList(["m1"], None), MessageList(["m1"], None)],
        messages={"m1": _gmail_message("m1", "admin@ztv.su")},
    )

    async def _run() -> None:
        async with db.get_session() as session:
            first = await import_messages(session, client)
            assert first.imported == 1
            second = await import_messages(session, client)
            assert second.duplicates == 1
            assert second.imported == 0
        rows = await _rows()
        assert len(rows) == 1

    asyncio.run(_run())


def test_import_malformed_message_failed(schema: None) -> None:
    client = FakeGmailClient(
        pages=[MessageList(["m1"], None)],
        errors={"m1": GmailError(message="boom")},
    )

    async def _run() -> None:
        async with db.get_session() as session:
            stats = await import_messages(session, client)
        assert stats.failed == 1
        assert stats.imported == 0
        assert await _rows() == []

    asyncio.run(_run())


def test_one_failed_message_does_not_rollback_others(schema: None) -> None:
    client = FakeGmailClient(
        pages=[MessageList(["A", "B", "C"], None)],
        messages={
            "A": _gmail_message("A", "support@liteserver.nl"),
            "C": _gmail_message("C", "admin@ztv.su"),
        },
        errors={"B": GmailError(message="boom")},
    )

    async def _run() -> None:
        async with db.get_session() as session:
            stats = await import_messages(session, client)
        assert stats.imported == 2
        assert stats.failed == 1
        rows = await _rows()
        assert {r.external_id for r in rows} == {"A", "C"}

    asyncio.run(_run())


def test_pagination(schema: None) -> None:
    client = FakeGmailClient(
        pages=[MessageList(["A"], "tok"), MessageList(["B"], None)],
        messages={
            "A": _gmail_message("A", "support@liteserver.nl"),
            "B": _gmail_message("B", "admin@ztv.su"),
        },
    )

    async def _run() -> None:
        async with db.get_session() as session:
            stats = await import_messages(session, client)
        assert stats.imported == 2

    asyncio.run(_run())


def test_max_messages_limit(schema: None) -> None:
    client = FakeGmailClient(
        pages=[MessageList(["A", "B", "C"], None)],
        messages={
            "A": _gmail_message("A", "support@liteserver.nl"),
            "B": _gmail_message("B", "admin@ztv.su"),
            "C": _gmail_message("C", "admin@ztv.su"),
        },
    )

    async def _run() -> None:
        async with db.get_session() as session:
            stats = await import_messages(session, client, max_messages=2)
        assert stats.found == 2
        assert stats.imported == 2

    asyncio.run(_run())


def test_auth_error_401_aborts(schema: None) -> None:
    client = FakeGmailClient(
        pages=[MessageList(["A"], None)],
        errors={"A": GmailError(message="unauthorized", http_status=401)},
    )

    async def _run() -> None:
        async with db.get_session() as session:
            with pytest.raises(GmailError):
                await import_messages(session, client)

    asyncio.run(_run())


def test_missing_subject_stored_as_empty_string(schema: None) -> None:
    message = _gmail_message("m1", "admin@ztv.su")
    message["payload"]["headers"] = [{"name": "From", "value": "admin@ztv.su"}]
    client = FakeGmailClient(pages=[MessageList(["m1"], None)], messages={"m1": message})

    async def _run() -> None:
        async with db.get_session() as session:
            await import_messages(session, client)
        rows = await _rows()
        assert rows[0].subject == ""

    asyncio.run(_run())


def test_default_query_lists_only_whitelisted_senders(schema: None) -> None:
    client = FakeGmailClient(pages=[MessageList([], None)])

    async def _run() -> None:
        async with db.get_session() as session:
            await import_messages(session, client)

    asyncio.run(_run())
    assert client.queries == ["from:(admin@ztv.su OR support@liteserver.nl)"]


def test_get_timeout_is_per_message_failure(schema: None) -> None:
    """A socket timeout on one message must not abort the rest of the batch."""

    class TimeoutService:
        def users(self):
            return self

        def messages(self):
            return self

        def get(self, **kwargs):

            class _Req:
                def execute(self):
                    if kwargs["id"] == "A":
                        raise TimeoutError("timed out")
                    return _gmail_message(kwargs["id"], "admin@ztv.su")

            return _Req()

    from app.gmail.client import GmailClient

    real = GmailClient(credentials=None, service=TimeoutService())
    client = FakeGmailClient(pages=[MessageList(["A", "B"], None)])
    client.get_message = real.get_message  # type: ignore[method-assign]

    async def _run() -> None:
        async with db.get_session() as session:
            stats = await import_messages(session, client)
        assert (stats.failed, stats.imported) == (1, 1)
        assert [r.external_id for r in await _rows()] == ["B"]

    asyncio.run(_run())


def test_malformed_payload_does_not_abort_batch(schema: None) -> None:
    malformed = {
        "id": "B",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": ["not-a-dict"],
            "parts": ["junk"],
        },
    }
    client = FakeGmailClient(
        pages=[MessageList(["A", "B", "C"], None)],
        messages={
            "A": _gmail_message("A", "support@liteserver.nl"),
            "B": malformed,
            "C": _gmail_message("C", "admin@ztv.su"),
        },
    )

    async def _run() -> None:
        async with db.get_session() as session:
            stats = await import_messages(session, client)
        assert (stats.imported, stats.failed) == (2, 1)
        assert {r.external_id for r in await _rows()} == {"A", "C"}

    asyncio.run(_run())


def test_auth_error_403_aborts_but_keeps_earlier_imports(schema: None) -> None:
    client = FakeGmailClient(
        pages=[MessageList(["A", "B"], None)],
        messages={"A": _gmail_message("A", "admin@ztv.su")},
        errors={"B": GmailError(message="forbidden", http_status=403)},
    )

    async def _run() -> None:
        async with db.get_session() as session:
            with pytest.raises(GmailError):
                await import_messages(session, client)
        assert [r.external_id for r in await _rows()] == ["A"]

    asyncio.run(_run())


def test_list_error_on_second_page_keeps_first_page(schema: None) -> None:
    class FailingSecondPage(FakeGmailClient):
        async def list_messages(self, *, q=None, max_results=None, page_token=None):
            if page_token is not None:
                raise GmailError(message="server error", http_status=500)
            return MessageList(["A"], "tok")

    client = FailingSecondPage(messages={"A": _gmail_message("A", "admin@ztv.su")})

    async def _run() -> None:
        async with db.get_session() as session:
            with pytest.raises(GmailError):
                await import_messages(session, client)
        assert [r.external_id for r in await _rows()] == ["A"]

    asyncio.run(_run())


def test_db_error_propagates_and_keeps_earlier_imports(
    schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import inbox

    real_create = inbox.create_message

    async def flaky_create(session, **kwargs):
        if kwargs["external_id"] == "B":
            raise RuntimeError("disk I/O error")
        return await real_create(session, **kwargs)

    monkeypatch.setattr(inbox, "create_message", flaky_create)
    client = FakeGmailClient(
        pages=[MessageList(["A", "B"], None)],
        messages={
            "A": _gmail_message("A", "admin@ztv.su"),
            "B": _gmail_message("B", "admin@ztv.su"),
        },
    )

    async def _run() -> None:
        async with db.get_session() as session:
            with pytest.raises(RuntimeError):
                await import_messages(session, client)
        assert [r.external_id for r in await _rows()] == ["A"]

    asyncio.run(_run())


def test_duplicate_is_not_downloaded_again(schema: None) -> None:
    class CountingClient(FakeGmailClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.fetched: list[str] = []

        async def get_message(self, message_id):
            self.fetched.append(message_id)
            return await super().get_message(message_id)

    client = CountingClient(
        pages=[MessageList(["A"], None), MessageList(["A"], None)],
        messages={"A": _gmail_message("A", "admin@ztv.su")},
    )

    async def _run() -> None:
        async with db.get_session() as session:
            await import_messages(session, client)
            second = await import_messages(session, client)
        assert second.duplicates == 1

    asyncio.run(_run())
    assert client.fetched == ["A"]


def test_max_messages_enforced_even_if_api_ignores_max_results(schema: None) -> None:
    class IgnoresMaxResults(FakeGmailClient):
        async def list_messages(self, *, q=None, max_results=None, page_token=None):
            return MessageList(["A", "B", "C"], None)

    client = IgnoresMaxResults(
        messages={m: _gmail_message(m, "admin@ztv.su") for m in ("A", "B", "C")}
    )

    async def _run() -> None:
        async with db.get_session() as session:
            stats = await import_messages(session, client, max_messages=2)
        assert (stats.found, stats.imported) == (2, 2)

    asyncio.run(_run())


def test_non_whitelisted_sender_address_not_logged(
    schema: None, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeGmailClient(
        pages=[MessageList(["m1"], None)],
        messages={"m1": _gmail_message("m1", "private.person@example.com")},
    )

    async def _run() -> None:
        async with db.get_session() as session:
            await import_messages(session, client)

    with caplog.at_level("INFO", logger="app.gmail.importer"):
        asyncio.run(_run())
    assert "private.person@example.com" not in caplog.text


def test_spoofed_display_name_is_not_imported(schema: None) -> None:
    """An allowed address inside the display name must not pass the whitelist,
    so the message never reaches classification or Telegram notifications."""

    message = _gmail_message("spoof", "unused")
    message["payload"]["headers"][0]["value"] = "support@liteserver.nl <attacker@evil.example>"
    client = FakeGmailClient(pages=[MessageList(["spoof"], None)], messages={"spoof": message})

    async def _run() -> None:
        async with db.get_session() as session:
            stats = await import_messages(session, client)
        assert stats.skipped_not_allowed == 1 and stats.imported == 0
        assert await _rows() == []

    asyncio.run(_run())
