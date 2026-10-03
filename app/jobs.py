"""Scheduled job bodies and their registration.

Each job only orchestrates existing domain functions (events, reminders,
expenses, Sheets sync, Gmail import, inbox classification); the business rules
stay in those modules. Jobs raise on failure: the scheduler wrapper records the
failure in ``JobRun`` and sends at most one Telegram alert per failure streak.

Google Sheets consistency: import (Sheets -> SQLite) must run *before* any
export (SQLite -> Sheets) so user edits are applied first, and a job that
changes the database must export afterwards so the next import does not revert
the change with stale sheet values. Export merges, so rows that failed import
(or are not imported yet) are never overwritten. Every job that writes events/expenses
therefore holds ``Services.data_lock`` for its whole import -> change -> export.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import partial

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, events, expenses, job_runs, notification_outbox, reminders
from app.config import Settings
from app.deepseek.client import DeepSeekClient
from app.gmail.client import GmailClient, create_client_from_settings as create_gmail_client
from app.gmail.importer import import_messages
from app.google_sheets.client import GoogleSheetsClient
from app.google_sheets.setup import ensure_workbook
from app.google_sheets.sync import (
    SheetValidationError,
    export_events,
    export_expenses,
    export_reminders,
    import_events,
    import_expenses,
)
from app.inbox_processing import process_unprocessed
from app.notifications import notify_job_interrupted
from app.scheduled_notifications import queue_event_reminders, queue_payment_reminders
from app.scheduler import JobSpec
from app.telegram.client import TelegramClient

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = timedelta(minutes=15)
GMAIL_IMPORT_INTERVAL = timedelta(minutes=15)
INBOX_CLASSIFICATION_INTERVAL = timedelta(minutes=15)
SHEETS_SYNC_INTERVAL = timedelta(minutes=30)
# Daily reminders run at 09:00 UTC (12:00 MSK / 11:00 CEST). After a restart
# during the day they also run once immediately; at night they wait for 09:00.
DAILY_REMINDERS_HOUR_UTC = 9
DAYTIME_END_HOUR_UTC = 21
INBOX_BATCH_SIZE = 20
NOTIFICATION_INTERVAL = timedelta(minutes=1)
NOTIFICATION_BATCH_SIZE = 20
# Finished job runs older than this are deleted by the health check.
JOB_RUN_RETENTION = timedelta(days=30)


class SheetsSyncError(RuntimeError):
    """Some sheet rows were invalid; they were skipped and left untouched."""


@dataclass
class Services:
    """Long-lived clients shared by jobs. ``None`` means "not configured"."""

    settings: Settings
    telegram: TelegramClient | None = None
    sheets: GoogleSheetsClient | None = None
    deepseek: DeepSeekClient | None = None
    gmail_factory: Callable[[Settings], GmailClient | None] = create_gmail_client
    data_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def chat_id(self) -> int | None:
        return self.settings.telegram_chat_id

    async def aclose(self) -> None:
        """Close every async client (each close is attempted independently)."""

        for name, client in (("telegram", self.telegram), ("deepseek", self.deepseek)):
            if client is None:
                continue
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - shutdown must close the rest
                logger.exception("Failed to close %s client", name)


# --- health check / stale job recovery ----------------------------------------


async def recover_stale_jobs_and_notify(
    timeout_seconds: int,
    *,
    telegram: TelegramClient | None = None,
    chat_id: int | None = None,
) -> int:
    """Mark stale ``running`` jobs as failed and alert once per recovered job."""

    timeout = timedelta(seconds=timeout_seconds)
    async with db.get_session() as session:
        recovered = await job_runs.recover_stale_jobs(session, timeout=timeout)
        await session.commit()
        if telegram is not None and chat_id is not None:
            for run in recovered:
                await notify_job_interrupted(
                    session, run, telegram=telegram, chat_id=chat_id
                )
    return len(recovered)


async def health_check(services: Services) -> None:
    """Detect jobs stuck in ``running`` (crash/timeout) and mark them failed."""

    recovered = await recover_stale_jobs_and_notify(
        services.settings.job_stale_timeout_seconds,
        telegram=services.telegram,
        chat_id=services.chat_id,
    )
    if recovered:
        logger.warning("health_check: marked %d stale job run(s) as failed", recovered)
    async with db.get_session() as session:
        pruned = await job_runs.prune_finished(session, older_than=JOB_RUN_RETENTION)
        await session.commit()
    logger.info("health_check: ok (%d old job run(s) pruned)", pruned)


# --- Google Sheets --------------------------------------------------------------


async def _import_from_sheets(
    session: AsyncSession, sheets: GoogleSheetsClient
) -> dict[str, list[SheetValidationError]]:
    """Apply user rows from Sheets (create/update) and commit; return row errors.

    Invalid rows are skipped and reported; every valid row is still applied.
    """

    problems: dict[str, list[SheetValidationError]] = {}
    for sheet, importer in (("Events", import_events), ("Expenses", import_expenses)):
        errors = await importer(session, sheets)
        await session.commit()
        if errors:
            problems[sheet] = errors
    return problems


async def _export_to_sheets(
    session: AsyncSession,
    sheets: GoogleSheetsClient,
    problems: dict[str, list[SheetValidationError]],
) -> None:
    """Merge the canonical state into Sheets; rows that failed import stay as typed."""

    def preserved(sheet: str) -> set:
        return {error.key for error in problems.get(sheet, []) if error.key is not None}

    await export_events(session, sheets, preserve=preserved("Events"))
    await export_expenses(session, sheets, preserve=preserved("Expenses"))
    await export_reminders(session, sheets)


def _raise_for_sheet_errors(problems: dict[str, list[SheetValidationError]]) -> None:
    if problems:
        summary = ", ".join(f"{sheet} ({len(errors)})" for sheet, errors in problems.items())
        raise SheetsSyncError(
            f"Invalid rows in sheet(s) {summary}; they were skipped and left as typed. "
            "Fix them in Google Sheets (details in the application log)"
        )


async def sheets_sync(services: Services) -> None:
    """Bootstrap the workbook, import user edits, then export the current state."""

    sheets = services.sheets
    assert sheets is not None
    async with services.data_lock, db.get_session() as session:
        await ensure_workbook(sheets)
        problems = await _import_from_sheets(session, sheets)
        await _export_to_sheets(session, sheets, problems)
    _raise_for_sheet_errors(problems)


# --- reminders / daily maintenance ---------------------------------------------


async def _ensure_upcoming_reminders(session: AsyncSession, today: date) -> int:
    """Create missing reminders for active, upcoming events (idempotent).

    ``create_event`` does not generate reminders, so an event that was never
    edited would otherwise never be reminded about. Past one-off events are
    skipped so no stale reminders are created for them.
    """

    created = 0
    for event in await events.list_events(session, active_only=True):
        if event.next_date >= today:
            created += len(await reminders.generate_reminders(session, event))
    await session.commit()
    return created


async def daily_reminders(services: Services, today: date | None = None) -> None:
    """Queue due event and payment reminders and roll recurring items forward.

    Nothing is sent from here: reminders go to the notification outbox in the
    same transaction that marks them handled, and ``deliver_notifications``
    sends them. A Telegram outage therefore delays reminders but loses none.
    """

    today = today or date.today()
    async with services.data_lock, db.get_session() as session:
        problems: dict[str, list[SheetValidationError]] = {}
        if services.sheets is not None:
            problems = await _import_from_sheets(session, services.sheets)

        created = await _ensure_upcoming_reminders(session, today)
        # Queue before advancing: advancing a yearly event regenerates (deletes)
        # its unqueued reminders.
        queued_events = await queue_event_reminders(session, today)
        await session.commit()
        advanced_events = await events.advance_due_events(session, today=today)
        advanced_expenses = await expenses.advance_due_expenses(session, today=today)
        await session.commit()
        # After advancing, so the reminder targets the current payment cycle.
        queued_payments = await queue_payment_reminders(session, today)
        await session.commit()
        logger.info(
            "daily_reminders: %d reminder(s) created, %d event and %d payment "
            "reminder(s) queued, %d event(s) and %d expense(s) advanced",
            created,
            queued_events,
            queued_payments,
            len(advanced_events),
            len(advanced_expenses),
        )

        if services.sheets is not None:
            await _export_to_sheets(session, services.sheets, problems)
    _raise_for_sheet_errors(problems)


# --- notification outbox ----------------------------------------------------------


async def deliver_notifications(services: Services) -> None:
    """Send due outbox notifications via Telegram (at-least-once, with retries).

    A Telegram failure is recorded on the notification itself (``last_error``,
    backoff) and is not a job failure, so an outage does not trigger job-failure
    alerts and this job can never produce notifications about itself.
    """

    assert services.telegram is not None and services.chat_id is not None
    delivered, failed = await notification_outbox.deliver_due(
        services.telegram, services.chat_id, limit=NOTIFICATION_BATCH_SIZE
    )
    if delivered or failed:
        logger.info("deliver_notifications: %d delivered, %d failed", delivered, failed)


# --- Gmail / inbox ----------------------------------------------------------------


async def gmail_import(services: Services) -> None:
    """Import whitelisted Gmail messages into the inbox (read-only).

    The client is built per run from the existing token file (never an OAuth
    browser flow), so a token fixed on disk is picked up without a restart.
    Building it reads/refreshes the token synchronously, hence the thread.
    """

    client = await asyncio.to_thread(services.gmail_factory, services.settings)
    if client is None:
        return
    try:
        async with db.get_session() as session:
            stats = await import_messages(
                session, client, max_messages=services.settings.gmail_max_messages
            )
    finally:
        await asyncio.to_thread(client.close)
    logger.info("gmail_import: %s", stats)


async def inbox_classification(services: Services) -> None:
    """Classify new inbox messages with DeepSeek (results validated in Python)."""

    deepseek = services.deepseek
    assert deepseek is not None
    async with db.get_session() as session:
        processed = await process_unprocessed(session, deepseek, limit=INBOX_BATCH_SIZE)
    logger.info("inbox_classification: %d message(s) processed", len(processed))


# --- registration -----------------------------------------------------------------


def _every(interval: timedelta) -> IntervalTrigger:
    return IntervalTrigger(seconds=interval.total_seconds(), timezone=timezone.utc)


def _is_daytime(now: datetime) -> bool:
    return DAILY_REMINDERS_HOUR_UTC <= now.hour < DAYTIME_END_HOUR_UTC


def build_jobs(services: Services, *, now: datetime | None = None) -> list[JobSpec]:
    """Return the jobs to schedule; integrations that are not configured are skipped."""

    now = now or datetime.now(timezone.utc)
    settings = services.settings
    jobs = [
        JobSpec(
            "health_check",
            partial(health_check, services),
            _every(HEALTH_CHECK_INTERVAL),
        ),
        JobSpec(
            "daily_reminders",
            partial(daily_reminders, services),
            CronTrigger(hour=DAILY_REMINDERS_HOUR_UTC, minute=0, timezone=timezone.utc),
            run_at_startup=_is_daytime(now),
        ),
    ]
    if services.telegram is not None and services.chat_id is not None:
        jobs.append(
            JobSpec(
                "deliver_notifications",
                partial(deliver_notifications, services),
                _every(NOTIFICATION_INTERVAL),
                run_at_startup=True,
            )
        )
    if services.sheets is not None:
        jobs.append(
            JobSpec(
                "sheets_sync",
                partial(sheets_sync, services),
                _every(SHEETS_SYNC_INTERVAL),
                run_at_startup=True,
            )
        )
    if settings.gmail_enabled:
        jobs.append(
            JobSpec(
                "gmail_import",
                partial(gmail_import, services),
                _every(GMAIL_IMPORT_INTERVAL),
                run_at_startup=True,
            )
        )
    if services.deepseek is not None:
        jobs.append(
            JobSpec(
                "inbox_classification",
                partial(inbox_classification, services),
                _every(INBOX_CLASSIFICATION_INTERVAL),
                run_at_startup=True,
            )
        )
    return jobs
