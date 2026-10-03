"""Telegram notification layer for important system events.

This is the only place that turns application state (a failed or interrupted
``JobRun``) into a Telegram message. The state layer (``app/job_runs.py``) stays
unaware of Telegram; it only tracks the channel-agnostic ``notification_sent_at``
marker that makes notifications idempotent.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import job_runs
from app.models.job_run import JobRun
from app.telegram.client import TelegramAPIError, TelegramClient

logger = logging.getLogger(__name__)

JOB_FAILED_TITLE = "🔴 Job failed"
JOB_INTERRUPTED_TITLE = "⚠️ Job interrupted"


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.strftime("%d.%m.%Y %H:%M") + " UTC"


def format_notification(title: str, message: str) -> str:
    """Combine a title and a body into the plain-text message to send."""

    return f"{title}\n\n{message}"


def _format_job_failure_message(run: JobRun) -> str:
    return (
        f"{run.job_name}\n\n"
        f"Ошибка:\n{run.error or 'unknown error'}\n\n"
        f"Время:\n{_format_timestamp(run.finished_at)}"
    )


def _format_job_interrupted_message(run: JobRun) -> str:
    return (
        f"{run.job_name}\n\n"
        "Задача была прервана после остановки процесса.\n\n"
        f"Время:\n{_format_timestamp(run.finished_at)}"
    )


async def notify(
    telegram: TelegramClient | None,
    chat_id: int | None,
    *,
    title: str,
    message: str,
) -> bool:
    """Send a plain-text notification via Telegram.

    Returns ``True`` if a message was actually sent, ``False`` if Telegram is
    not configured or the send failed. Telegram errors are logged but never
    raised here, so they cannot affect application/domain state.
    """

    if telegram is None or chat_id is None:
        return False
    try:
        await telegram.send_message(
            chat_id=chat_id, text=format_notification(title, message)
        )
    except TelegramAPIError as exc:
        logger.warning("Telegram notification not sent (%s): %s", title, exc)
        return False
    return True


async def _send_job_notification(
    session: AsyncSession,
    run: JobRun,
    *,
    telegram: TelegramClient | None,
    chat_id: int | None,
    title: str,
    message: str,
) -> bool:
    """Send a job notification once and record delivery on ``JobRun``.

    The order is deliberate: the caller has already committed ``run`` as
    ``failed``; we send Telegram, and only after a successful response mark
    ``notification_sent_at``. If Telegram is unavailable the marker stays
    ``NULL``, so the notification is not recorded as delivered.
    """

    if run.notification_sent_at is not None:
        return False
    sent = await notify(telegram, chat_id, title=title, message=message)
    if sent:
        await job_runs.mark_notification_sent(session, run)
        await session.commit()
    return sent


async def notify_job_failed(
    session: AsyncSession,
    run: JobRun,
    *,
    telegram: TelegramClient | None,
    chat_id: int | None,
) -> bool:
    """Notify about a job that transitioned to ``failed``."""

    return await _send_job_notification(
        session,
        run,
        telegram=telegram,
        chat_id=chat_id,
        title=JOB_FAILED_TITLE,
        message=_format_job_failure_message(run),
    )


async def notify_job_interrupted(
    session: AsyncSession,
    run: JobRun,
    *,
    telegram: TelegramClient | None,
    chat_id: int | None,
) -> bool:
    """Notify about a stale ``running`` job recovered as ``failed``."""

    return await _send_job_notification(
        session,
        run,
        telegram=telegram,
        chat_id=chat_id,
        title=JOB_INTERRUPTED_TITLE,
        message=_format_job_interrupted_message(run),
    )


def make_failure_notifier(telegram: TelegramClient | None, chat_id: int | None):
    """Return a ``(session, run) -> None`` notifier for the scheduler."""

    async def _notify(session: AsyncSession, run: JobRun) -> None:
        await notify_job_failed(session, run, telegram=telegram, chat_id=chat_id)

    return _notify
