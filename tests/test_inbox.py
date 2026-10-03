"""Tests for the Inbox intake layer (idempotent create + state transitions)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.db as db
import app.inbox as inbox
from app.models.inbox import InboxMessage, InboxStatus


def _run(coro) -> None:
    asyncio.run(coro)


def test_create_message(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            msg = await inbox.create_message(
                s,
                source="gmail",
                external_id="msg-1",
                sender="a@example.com",
                subject="Тема",
                body="Текст",
            )
            await s.commit()
            assert msg.status is InboxStatus.NEW
            assert msg.sender == "a@example.com"
            assert msg.subject == "Тема"
            assert msg.classification is None
            assert msg.processed_at is None

    _run(_r())


def test_create_message_is_idempotent(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            first = await inbox.create_message(
                s, source="gmail", external_id="msg-1", subject="Old"
            )
            await s.commit()
            first_id = first.id

        async with db.get_session() as s:
            second = await inbox.create_message(
                s, source="gmail", external_id="msg-1", subject="New"
            )
            await s.commit()
            assert second.id == first_id
            assert second.subject == "Old"  # existing row is returned unchanged

        async with db.get_session() as s:
            count = (await s.execute(text("SELECT COUNT(*) FROM inbox_messages"))).scalar()
            assert count == 1

    _run(_r())


def test_get_message(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            msg = await inbox.create_message(s, source="gmail", external_id="e1")
            await s.commit()
            mid = msg.id

            loaded = await inbox.get_message(s, mid)
            assert loaded is not None
            assert loaded.external_id == "e1"
            assert await inbox.get_message(s, 999_999) is None

    _run(_r())


def test_list_unprocessed(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            a = await inbox.create_message(s, source="gmail", external_id="a")
            b = await inbox.create_message(s, source="gmail", external_id="b")
            await inbox.mark_processing(s, b)
            await s.commit()

            pending = await inbox.list_unprocessed(s)
            assert [m.id for m in pending] == [a.id]

    _run(_r())


def test_status_transitions_to_processed(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            msg = await inbox.create_message(s, source="gmail", external_id="e1")
            await inbox.mark_processing(s, msg)
            await inbox.mark_processed(
                s, msg, classification="financial", metadata={"summary": "x"}
            )
            await s.commit()
            assert msg.status is InboxStatus.PROCESSED
            assert msg.classification == "financial"
            assert msg.metadata_ == {"summary": "x"}
            assert msg.processed_at is not None

    _run(_r())


def test_status_transitions_to_failed(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            msg = await inbox.create_message(s, source="gmail", external_id="e1")
            await inbox.mark_processing(s, msg)
            await inbox.mark_failed(s, msg, error="timeout")
            await s.commit()
            assert msg.status is InboxStatus.FAILED
            assert msg.metadata_ == {"error": "timeout"}
            assert msg.classification is None
            assert msg.processed_at is None

    _run(_r())


def test_invalid_transitions_rejected(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            new = await inbox.create_message(s, source="gmail", external_id="e1")
            with pytest.raises(ValueError):
                await inbox.mark_processed(s, new)  # new -> processed
            with pytest.raises(ValueError):
                await inbox.mark_failed(s, new)  # new -> failed

            proc = await inbox.create_message(s, source="gmail", external_id="e2")
            await inbox.mark_processing(s, proc)
            await inbox.mark_processed(s, proc)
            with pytest.raises(ValueError):
                await inbox.mark_processing(s, proc)  # processed -> processing
            with pytest.raises(ValueError):
                await inbox.mark_failed(s, proc)  # processed -> failed

    _run(_r())


def test_reprocess_failed_message(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            msg = await inbox.create_message(s, source="gmail", external_id="e1")
            await inbox.mark_processing(s, msg)
            await inbox.mark_failed(s, msg, error="boom")
            # failed -> processing -> processed is allowed (reprocessing).
            await inbox.mark_processing(s, msg)
            await inbox.mark_processed(s, msg, classification="other")
            await s.commit()
            assert msg.status is InboxStatus.PROCESSED
            assert msg.classification == "other"

    _run(_r())


def test_unique_constraint_enforced_at_db_level(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            await inbox.create_message(s, source="gmail", external_id="dup")
            await s.commit()
            with pytest.raises(IntegrityError):
                await s.execute(
                    text(
                        "INSERT INTO inbox_messages "
                        "(source, external_id, status, created_at) "
                        "VALUES ('gmail', 'dup', 'new', '2026-01-01 00:00:00')"
                    )
                )

    _run(_r())


def test_invalid_status_rejected_at_db_level(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            with pytest.raises(IntegrityError):
                await s.execute(
                    text(
                        "INSERT INTO inbox_messages "
                        "(source, external_id, status, created_at) "
                        "VALUES ('gmail', 'x', 'weird', '2026-01-01 00:00:00')"
                    )
                )

    _run(_r())
