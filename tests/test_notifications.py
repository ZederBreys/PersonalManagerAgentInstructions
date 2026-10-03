"""Tests for the Telegram notification layer and its JobRun integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import app.db as db
import app.job_runs as job_runs
from app.db import utcnow
from app.main import run_startup_recovery
from app.models.job_run import JobRun, JobRunStatus
from app.notifications import make_failure_notifier, notify, notify_job_failed
from app.scheduler import _execute_job
from app.telegram.client import TelegramAPIError

CHAT_ID = 123456789


class FakeTelegram:
    """In-memory stand-in for ``TelegramClient`` (no real HTTP)."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id: int, text: str) -> dict:
        if self.fail:
            raise TelegramAPIError(description="boom")
        self.sent.append((chat_id, text))
        return {"message_id": 1}


def _run(coro) -> None:
    asyncio.run(coro)


async def _failed_run(session) -> JobRun:
    run = await job_runs.create_job_run(session, job_name="daily_agent")
    await job_runs.start_job_run(session, run)
    await job_runs.fail_job_run(session, run, error="RuntimeError: boom")
    await session.commit()
    return run


async def _boom() -> None:
    raise RuntimeError("boom")


def test_notify_sends_when_configured() -> None:
    async def _r() -> None:
        fake = FakeTelegram()
        result = await notify(fake, CHAT_ID, title="T", message="M")
        assert result is True
        assert len(fake.sent) == 1
        chat_id, text = fake.sent[0]
        assert chat_id == CHAT_ID
        assert text == "T\n\nM"

    _run(_r())


def test_notify_skips_when_not_configured() -> None:
    async def _r() -> None:
        fake = FakeTelegram()
        assert await notify(None, CHAT_ID, title="T", message="M") is False
        assert await notify(fake, None, title="T", message="M") is False
        assert fake.sent == []

    _run(_r())


def test_notify_returns_false_on_telegram_error() -> None:
    async def _r() -> None:
        fake = FakeTelegram(fail=True)
        result = await notify(fake, CHAT_ID, title="T", message="M")
        assert result is False
        assert fake.sent == []

    _run(_r())


def test_job_failure_sends_notification(schema) -> None:
    async def _r() -> None:
        fake = FakeTelegram()
        notifier = make_failure_notifier(fake, CHAT_ID)
        await _execute_job("daily_agent", _boom, notifier=notifier)

        assert len(fake.sent) == 1
        chat_id, text = fake.sent[0]
        assert chat_id == CHAT_ID
        assert "Job failed" in text
        assert "daily_agent" in text
        assert "RuntimeError: boom" in text

        async with db.get_session() as session:
            run = (await job_runs.get_latest_job_runs(session, job_name="daily_agent", limit=1))[0]
            assert run.status is JobRunStatus.FAILED
            assert run.notification_sent_at is not None

    _run(_r())


def test_notify_job_failed_is_idempotent(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as session:
            run = await _failed_run(session)
            run_id = run.id

        async with db.get_session() as session:
            run = await session.get(JobRun, run_id)
            first = await notify_job_failed(session, run, telegram=FakeTelegram(), chat_id=CHAT_ID)
            assert first is True

        async with db.get_session() as session:
            run = await session.get(JobRun, run_id)
            assert run.notification_sent_at is not None
            second_fake = FakeTelegram()
            second = await notify_job_failed(session, run, telegram=second_fake, chat_id=CHAT_ID)
            assert second is False
            assert second_fake.sent == []

    _run(_r())


def test_notify_job_failed_keeps_state_when_telegram_fails(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as session:
            run = await _failed_run(session)
            run_id = run.id

        async with db.get_session() as session:
            run = await session.get(JobRun, run_id)
            fake = FakeTelegram(fail=True)
            result = await notify_job_failed(session, run, telegram=fake, chat_id=CHAT_ID)
            assert result is False
            assert run.status is JobRunStatus.FAILED
            assert run.notification_sent_at is None

    _run(_r())


def test_startup_recovery_sends_notification(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as session:
            run = await job_runs.create_job_run(session, job_name="daily_agent")
            await job_runs.start_job_run(session, run)
            run.started_at = utcnow() - timedelta(days=2)
            await session.commit()
            run_id = run.id

        fake = FakeTelegram()
        recovered = await run_startup_recovery(3600, telegram=fake, chat_id=CHAT_ID)
        assert recovered == 1
        assert len(fake.sent) == 1
        _, text = fake.sent[0]
        assert "Job interrupted" in text
        assert "daily_agent" in text

        async with db.get_session() as session:
            run = await session.get(JobRun, run_id)
            assert run.status is JobRunStatus.FAILED
            assert run.notification_sent_at is not None

    _run(_r())


def test_startup_recovery_succeeds_when_telegram_unavailable(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as session:
            run = await job_runs.create_job_run(session, job_name="daily_agent")
            await job_runs.start_job_run(session, run)
            run.started_at = utcnow() - timedelta(days=2)
            await session.commit()
            run_id = run.id

        fake = FakeTelegram(fail=True)
        recovered = await run_startup_recovery(3600, telegram=fake, chat_id=CHAT_ID)
        assert recovered == 1

        async with db.get_session() as session:
            run = await session.get(JobRun, run_id)
            assert run.status is JobRunStatus.FAILED
            assert run.notification_sent_at is None

    _run(_r())
