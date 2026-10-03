"""Tests for the Inbox + DeepSeek processing service (fake classifier)."""

from __future__ import annotations

import asyncio

import app.db as db
import app.inbox as inbox
from app.deepseek import Classification, DeepSeekError
from app.inbox_processing import process_message, process_unprocessed
from app.models.inbox import InboxMessage, InboxStatus


class FakeDeepSeek:
    """Stand-in for DeepSeekClient; returns a result or raises a DeepSeekError."""

    def __init__(
        self,
        result: Classification | None = None,
        error: DeepSeekError | None = None,
    ) -> None:
        self.result = result or Classification(
            category="informational",
            importance="low",
            summary="summary",
            action_required=False,
        )
        self.error = error
        self.calls: list[dict[str, str | None]] = []

    async def classify_message(self, *, subject=None, body=None):
        self.calls.append({"subject": subject, "body": body})
        if self.error is not None:
            raise self.error
        return self.result


def _run(coro) -> None:
    asyncio.run(coro)


async def _make_message(session, *, external_id="e1", subject="S", body="B"):
    return await inbox.create_message(
        session, source="gmail", external_id=external_id, subject=subject, body=body
    )


def test_successful_classification_marks_processed(schema) -> None:
    fake = FakeDeepSeek(
        Classification(
            category="financial",
            importance="high",
            summary="Счёт",
            action_required=True,
        )
    )

    async def _r() -> None:
        async with db.get_session() as s:
            msg = await _make_message(s)
            await s.commit()
            mid = msg.id

        async with db.get_session() as s:
            loaded = await inbox.get_message(s, mid)
            result = await process_message(s, loaded, fake)
            assert result.status is InboxStatus.PROCESSED

        async with db.get_session() as s:
            stored = await s.get(InboxMessage, mid)
            assert stored.status is InboxStatus.PROCESSED
            assert stored.classification == "financial"
            assert stored.processed_at is not None
            assert stored.metadata_ == {
                "category": "financial",
                "importance": "high",
                "summary": "Счёт",
                "action_required": True,
            }
            assert fake.calls == [{"subject": "S", "body": "B"}]

    _run(_r())


def test_deepseek_error_marks_failed_not_processed(schema) -> None:
    fake = FakeDeepSeek(error=DeepSeekError(message="timeout", http_status=504))

    async def _r() -> None:
        async with db.get_session() as s:
            msg = await _make_message(s)
            await s.commit()
            mid = msg.id

        async with db.get_session() as s:
            loaded = await inbox.get_message(s, mid)
            result = await process_message(s, loaded, fake)
            assert result.status is InboxStatus.FAILED

        async with db.get_session() as s:
            stored = await s.get(InboxMessage, mid)
            assert stored.status is InboxStatus.FAILED
            assert stored.classification is None
            assert stored.processed_at is None
            assert stored.metadata_ == {"error": "timeout"}

    _run(_r())


def test_reprocess_failed_message_via_service(schema) -> None:
    failing = FakeDeepSeek(error=DeepSeekError(message="boom"))
    succeeding = FakeDeepSeek(
        Classification(
            category="personal",
            importance="normal",
            summary="ok",
            action_required=False,
        )
    )

    async def _r() -> None:
        async with db.get_session() as s:
            msg = await _make_message(s)
            await s.commit()
            mid = msg.id

        async with db.get_session() as s:
            loaded = await inbox.get_message(s, mid)
            await process_message(s, loaded, failing)  # -> failed

        async with db.get_session() as s:
            loaded = await inbox.get_message(s, mid)
            assert loaded.status is InboxStatus.FAILED
            await process_message(s, loaded, succeeding)  # failed -> processed

        async with db.get_session() as s:
            stored = await s.get(InboxMessage, mid)
            assert stored.status is InboxStatus.PROCESSED
            assert stored.classification == "personal"

    _run(_r())


def test_process_unprocessed_processes_only_new(schema) -> None:
    fake = FakeDeepSeek()

    async def _r() -> None:
        async with db.get_session() as s:
            a = await _make_message(s, external_id="a")
            b = await _make_message(s, external_id="b")
            c = await _make_message(s, external_id="c")
            await inbox.mark_processing(s, c)
            await s.commit()

        async with db.get_session() as s:
            processed = await process_unprocessed(s, fake)
            assert {m.external_id for m in processed} == {"a", "b"}

        async with db.get_session() as s:
            for eid in ("a", "b"):
                msg = (await inbox._find_by_external_id(s, "gmail", eid))
                assert msg.status is InboxStatus.PROCESSED
            c_loaded = await inbox._find_by_external_id(s, "gmail", "c")
            assert c_loaded.status is InboxStatus.PROCESSING  # untouched

    _run(_r())
