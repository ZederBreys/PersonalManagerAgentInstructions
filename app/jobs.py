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
import time
from collections.abc import Awaitable, Callable
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
from app.google_sheets.client import GoogleSheetsClient, GoogleSheetsError
from app.google_sheets.feedback import apply_feedback
from app.google_sheets.mappers import EVENT_HEADERS, EXPENSE_HEADERS
from app.google_sheets.setup import (
    ROLE_EVENTS,
    ROLE_EXPENSES,
    ROLE_REMINDERS,
    SheetLayout,
    ensure_workbook,
)
from app.google_sheets.sync import (
    RowReport,
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


# Cheap change detection for the sheets: one read every few seconds; a real sync
# runs only after the user's edits have stopped changing between two reads.
SYNC_REQUEST_TIMEOUT = 120.0  # a requested sync that never started is requested again
POLL_PAUSE_AFTER_ERROR = 30.0  # seconds to back off after a Sheets API error
POLL_PAUSE_BAD_RANGE = 10.0  # a renamed tab (HTTP 400): look the tabs up again soon


class SheetsSyncError(RuntimeError):
    """Some sheet rows were invalid; they were skipped and left untouched.

    The failure is recorded in ``JobRun`` (and shown by ``/status``), but there is
    no Telegram alert (``alert = False``): the rows are marked red, with the
    reason in a note, in the sheet itself — and with fast syncs a half-typed row
    would otherwise alert on every pause.
    """

    alert = False


SheetSnapshot = tuple[tuple[tuple[str, ...], ...], ...]


@dataclass
class Services:
    """Long-lived clients shared by jobs. ``None`` means "not configured"."""

    settings: Settings
    telegram: TelegramClient | None = None
    sheets: GoogleSheetsClient | None = None
    deepseek: DeepSeekClient | None = None
    gmail_factory: Callable[[Settings], GmailClient | None] = create_gmail_client
    data_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # --- Google Sheets state (all in memory; the sheet itself is the source) ---
    sheet_layout: SheetLayout | None = None  # role -> sheet (current title and id)
    poll_applied: SheetSnapshot | None = None  # what the last sync left in the sheet
    poll_seen: SheetSnapshot | None = None  # what the previous poll saw
    poll_pause_until: float = 0.0  # time.monotonic() before which polling is paused
    sync_pending: bool = False  # a sync was requested and has not started yet
    sync_requested_at: float = float("-inf")
    request_sync: Callable[[], None] | None = None  # set by the application: run a sync now

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


_SHEET_IMPORTERS = ((ROLE_EVENTS, import_events), (ROLE_EXPENSES, import_expenses))


async def _import_from_sheets(
    session: AsyncSession,
    sheets: GoogleSheetsClient,
    layout: SheetLayout,
    snapshots: dict[str, list[list[object]]] | None = None,
) -> tuple[dict[str, list[SheetValidationError]], dict[str, list[RowReport]]]:
    """Apply user rows from Sheets (create/update/delete) and commit.

    Returns the row errors (keyed by the sheet's current title) and one report
    per row (keyed by role). Invalid rows are skipped; every valid row is applied.
    """

    problems: dict[str, list[SheetValidationError]] = {}
    reports: dict[str, list[RowReport]] = {}
    for role, importer in _SHEET_IMPORTERS:
        ref = layout[role]
        rows: list[RowReport] = []
        snapshot: list[list[object]] = []
        errors = await importer(session, sheets, sheet=ref.title, reports=rows, snapshot=snapshot)
        if snapshots is not None:
            snapshots[role] = snapshot
        await session.commit()
        reports[role] = rows
        if errors:
            problems[ref.title] = errors
    return problems, reports


async def _export_to_sheets(
    session: AsyncSession,
    sheets: GoogleSheetsClient,
    layout: SheetLayout,
    problems: dict[str, list[SheetValidationError]],
    reports: dict[str, list[RowReport]],
    snapshots: dict[str, list[list[object]]] | None = None,
) -> None:
    """Merge the canonical state into Sheets; rows that failed import stay as typed.

    ``snapshots`` are the rows the import read: a row the user edited since then
    is not overwritten (the next sync picks the edit up).
    """

    def preserved(role: str) -> set:
        return {e.key for e in problems.get(layout[role].title, []) if e.key is not None}

    def deleted(role: str) -> set[int]:
        return {r.row_number for r in reports.get(role, []) if r.deleted}

    await export_events(
        session, sheets, preserve=preserved(ROLE_EVENTS), sheet=layout[ROLE_EVENTS].title,
        blank_rows=deleted(ROLE_EVENTS), import_snapshot=(snapshots or {}).get(ROLE_EVENTS),
    )
    await export_expenses(
        session, sheets, preserve=preserved(ROLE_EXPENSES), sheet=layout[ROLE_EXPENSES].title,
        blank_rows=deleted(ROLE_EXPENSES), import_snapshot=(snapshots or {}).get(ROLE_EXPENSES),
    )
    await export_reminders(session, sheets, sheet=layout[ROLE_REMINDERS].title)


async def _roundtrip(
    services: Services,
    session: AsyncSession,
    between: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, list[SheetValidationError]]:
    """Resolve the sheets, import user edits, run ``between``, export, mark rows.

    The sheets are looked up by their hidden marker on every run, so renamed or
    reordered tabs are followed. ``between`` is the place for work that changes
    the database after the user's edits were applied and before the export.
    """

    sheets = services.sheets
    assert sheets is not None
    layout = await ensure_workbook(sheets)
    services.sheet_layout = layout
    snapshots: dict[str, list[list[object]]] = {}
    problems, reports = await _import_from_sheets(session, sheets, layout, snapshots)
    if between is not None:
        await between()
    await _export_to_sheets(session, sheets, layout, problems, reports, snapshots)
    try:
        await apply_feedback(session, sheets, layout, reports)
    except GoogleSheetsError as exc:  # cosmetic: never fail the sync because of it
        logger.warning("Could not write the row notes/colours to the sheet: %s", exc.message)
    return problems


MAX_REPORTED_ROW_ERRORS = 5


def _raise_for_sheet_errors(problems: dict[str, list[SheetValidationError]]) -> None:
    """Fail the job with the row-level reasons (recorded, but not alerted).

    The message becomes ``JobRun.error`` and is shown by ``/status``: it lists
    the first few rows with the reason for each.
    """

    if not problems:
        return
    details = [f"{sheet}: {error}" for sheet, errors in problems.items() for error in errors]
    shown = details[:MAX_REPORTED_ROW_ERRORS]
    if len(details) > len(shown):
        shown.append(f"... and {len(details) - len(shown)} more")
    raise SheetsSyncError(
        "Invalid rows were skipped and left as typed in the sheet:\n"
        + "\n".join(shown)
        + "\nFix them in Google Sheets; valid rows are imported anyway."
    )


def _last_column(headers: list[str]) -> str:
    return chr(ord("A") + len(headers) - 1)


async def _snapshot(services: Services) -> SheetSnapshot:
    """The user-visible tables as strings, read with ONE request."""

    layout = services.sheet_layout
    assert layout is not None and services.sheets is not None
    ranges = [
        f"{layout[ROLE_EVENTS].title}!A:{_last_column(EVENT_HEADERS)}",
        f"{layout[ROLE_EXPENSES].title}!A:{_last_column(EXPENSE_HEADERS)}",
    ]
    values = await services.sheets.batch_get(ranges)
    return tuple(tuple(tuple(str(cell) for cell in row) for row in rows) for rows in values)


async def _try_snapshot(services: Services) -> SheetSnapshot | None:
    """The sheet right now, or ``None`` when it cannot be read (yet)."""

    if services.sheet_layout is None:
        return None
    try:
        return await _snapshot(services)
    except GoogleSheetsError:
        return None


async def _remember_sheet_state(services: Services, before: SheetSnapshot | None) -> None:
    """After a sync: decide whether the sheet still needs another pass.

    ``before`` is the sheet as it was when the sync started. If it is identical
    to the sheet now, the sync changed nothing and nobody edited during it: the
    state is the baseline. If it differs, the sync wrote something (an ID, a
    canonical value) or — importantly — the user edited while it ran; either
    way no baseline is claimed, so the watcher runs one more (usually trivial)
    pass instead of losing an edit made during the sync.
    """

    try:
        after = await _snapshot(services)
    except GoogleSheetsError as exc:
        logger.warning("Could not read the sheet state after the sync: %s", exc.message)
        services.poll_applied = None
        return
    services.poll_applied = after if before == after else None
    services.poll_seen = after


async def sheets_sync(services: Services) -> None:
    """Resolve the sheets, import user edits, export the state, mark each row."""

    assert services.sheets is not None
    services.sync_pending = False  # the watcher may request the next one right away
    async with services.data_lock, db.get_session() as session:
        before = await _try_snapshot(services)
        problems = await _roundtrip(services, session)
        await _remember_sheet_state(services, before)
    _raise_for_sheet_errors(problems)


async def sheets_poll(services: Services) -> None:
    """Cheap watcher: one read every few seconds, a sync only after edits settle.

    An edit is acted on when the sheet differs from what the last sync left AND
    is identical to the previous poll, i.e. the user stopped typing. The sync
    itself is the normal recorded ``sheets_sync`` job (this watcher writes no
    ``JobRun``). The periodic ``sheets_sync`` stays as a safety net.
    """

    now = time.monotonic()
    if now < services.poll_pause_until or services.data_lock.locked():
        return
    try:
        if services.sheet_layout is None:
            services.sheet_layout = await ensure_workbook(services.sheets)
        snapshot = await _snapshot(services)
    except GoogleSheetsError as exc:
        # E.g. a tab was renamed (the cached title is stale) or the quota is hit.
        services.sheet_layout = None
        pause = POLL_PAUSE_BAD_RANGE if exc.http_status == 400 else POLL_PAUSE_AFTER_ERROR
        services.poll_pause_until = now + pause
        logger.warning("Sheet poll failed (%s); retrying in %.0fs", exc.message, pause)
        return

    if snapshot == services.poll_applied:
        services.poll_seen = snapshot
        return
    if snapshot != services.poll_seen:
        services.poll_seen = snapshot  # still being edited: wait for the next poll
        return
    if services.request_sync is None:
        return
    if services.sync_pending and now - services.sync_requested_at < SYNC_REQUEST_TIMEOUT:
        return  # already requested, the scheduler is about to start it
    services.sync_pending = True
    services.sync_requested_at = now
    logger.info("Sheet edits detected; syncing")
    services.request_sync()


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


async def _daily_work(session: AsyncSession, today: date) -> None:
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


async def daily_reminders(services: Services, today: date | None = None) -> None:
    """Queue due event and payment reminders and roll recurring items forward.

    Nothing is sent from here: reminders go to the notification outbox in the
    same transaction that marks them handled, and ``deliver_notifications``
    sends them. A Telegram outage therefore delays reminders but loses none.
    """

    today = today or date.today()
    problems: dict[str, list[SheetValidationError]] = {}
    async with services.data_lock, db.get_session() as session:
        if services.sheets is None:
            await _daily_work(session, today)
        else:
            before = await _try_snapshot(services)
            problems = await _roundtrip(services, session, between=partial(_daily_work, session, today))
            await _remember_sheet_state(services, before)
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
        jobs.append(
            JobSpec(
                "sheets_poll",
                partial(sheets_poll, services),
                IntervalTrigger(seconds=settings.sheets_poll_seconds, timezone=timezone.utc),
                track_runs=False,
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
