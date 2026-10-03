"""Tests for the production runtime: lifecycle, jobs, Telegram polling, shutdown.

Everything external is faked (no real Telegram, Google or DeepSeek); the
database is a real temporary SQLite schema.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

import app.main as main_module
from app import db, events, job_runs, reminders
from app.config import Settings
from app.gmail.auth import GmailAuthError
from app.google_sheets.mappers import EVENT_HEADERS, event_to_row
from app.jobs import (
    Services,
    deliver_notifications,
    SheetsSyncError,
    build_jobs,
    daily_reminders,
    gmail_import,
    sheets_sync,
)
from app.models.event import Event
from app.models.job_run import JobRun, JobRunStatus
from app.models.notification import Notification
from app.models.reminder import Reminder
from app.notifications import make_streak_failure_notifier
from app.scheduler import CANCELLED_ERROR, JobSpec, _execute_job, create_scheduler
from app.telegram import bot
from app.telegram.client import TelegramAPIError
from tests.test_google_sheets_two_way import SheetStore

CHAT_ID = 4242
NIGHT = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)
DAY = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)


class FakeTelegram:
    """In-memory Telegram: queued ``getUpdates`` results, then blocks like a long poll."""

    def __init__(self, batches=None, *, fail_send: bool = False) -> None:
        self.batches = list(batches or [])
        self.sent: list[tuple[int, str]] = []
        self.offsets: list[int | None] = []
        self.fail_send = fail_send
        self.closed = False

    async def get_updates(self, offset=None, timeout=None):
        self.offsets.append(offset)
        if self.batches:
            item = self.batches.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        await asyncio.sleep(3600)
        return []

    async def send_message(self, chat_id: int, text: str):
        if self.fail_send:
            raise TelegramAPIError(description="telegram down")
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    async def aclose(self) -> None:
        self.closed = True


FakeSheets = SheetStore  # the shared stateful in-memory spreadsheet


def _settings(**values) -> Settings:
    values.setdefault("telegram_chat_id", CHAT_ID)
    return Settings(_env_file=None, **values)


def _update(update_id: int, text: str, chat_id: int = CHAT_ID) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


async def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


async def _latest_run(name: str) -> JobRun | None:
    async with db.get_session() as session:
        runs = await job_runs.get_latest_job_runs(session, job_name=name, limit=1)
        return runs[0] if runs else None


def _other_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


# --- job registration -----------------------------------------------------------


def test_build_jobs_without_integrations() -> None:
    jobs = build_jobs(Services(settings=_settings()), now=NIGHT)
    assert [j.name for j in jobs] == ["health_check", "daily_reminders"]


def test_build_jobs_with_all_integrations() -> None:
    services = Services(
        settings=_settings(gmail_client_secret_file="c.json", gmail_token_file="t.json"),
        sheets=FakeSheets(),  # type: ignore[arg-type]
        deepseek=object(),  # type: ignore[arg-type]
    )
    jobs = {j.name: j for j in build_jobs(services, now=NIGHT)}
    assert set(jobs) == {
        "health_check",
        "daily_reminders",
        "sheets_sync",
        "sheets_poll",
        "gmail_import",
        "inbox_classification",
    }
    assert jobs["sheets_sync"].run_at_startup
    assert jobs["gmail_import"].run_at_startup
    # The watcher runs every few seconds and must not write a JobRun each time.
    assert jobs["sheets_poll"].track_runs is False
    assert jobs["sheets_poll"].trigger.interval.total_seconds() == 5
    assert all(j.track_runs for name, j in jobs.items() if name != "sheets_poll")


def test_daily_reminders_runs_at_startup_only_in_daytime() -> None:
    services = Services(settings=_settings())
    by_name = lambda now: {j.name: j for j in build_jobs(services, now=now)}  # noqa: E731
    assert by_name(DAY)["daily_reminders"].run_at_startup is True
    assert by_name(NIGHT)["daily_reminders"].run_at_startup is False


def test_every_job_is_single_instance_and_coalesced() -> None:
    services = Services(
        settings=_settings(gmail_client_secret_file="c.json", gmail_token_file="t.json"),
        sheets=FakeSheets(),  # type: ignore[arg-type]
        deepseek=object(),  # type: ignore[arg-type]
    )
    scheduler = create_scheduler(jobs=build_jobs(services, now=NIGHT))
    for job in scheduler.get_jobs():
        assert job.max_instances == 1
        assert job.coalesce is True


# --- lifecycle --------------------------------------------------------------------


def test_run_lifecycle_polls_telegram_runs_jobs_and_cleans_up(
    schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module, "build_jobs", lambda s: build_jobs(s, now=NIGHT))
    telegram = FakeTelegram([[_update(1, "/help")]])
    sheets = FakeSheets()
    services = Services(settings=_settings(), telegram=telegram, sheets=sheets)  # type: ignore[arg-type]

    async def _run() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(main_module.run(services.settings, stop=stop, services=services))
        # Telegram polling answered, and the startup sheets_sync job really ran.
        await _wait_for(lambda: telegram.sent)
        await _wait_for(lambda: len(sheets.roles) == 6)  # every sheet found/created and marked
        stop.set()
        await task
        assert _other_tasks() == []

    asyncio.run(_run())

    assert telegram.sent == [(CHAT_ID, bot.HELP_TEXT)]
    assert telegram.closed is True
    assert set(sheets.sheets) >= {"Events", "Expenses", "Reminders", "Settings", "Inbox", "Email"}

    async def _check() -> None:
        run = await _latest_run("sheets_sync")
        assert run is not None and run.status is JobRunStatus.SUCCESS

    asyncio.run(_check())


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_stops_application_cleanly(
    schema: None, monkeypatch: pytest.MonkeyPatch, sig: signal.Signals
) -> None:
    monkeypatch.setattr(main_module, "build_jobs", lambda s: build_jobs(s, now=NIGHT))
    telegram = FakeTelegram()
    services = Services(settings=_settings(), telegram=telegram)  # type: ignore[arg-type]
    previous = signal.getsignal(sig)

    async def _run() -> None:
        asyncio.get_running_loop().call_later(0.3, signal.raise_signal, sig)
        await asyncio.wait_for(main_module.run(services.settings, services=services), 10)
        assert _other_tasks() == []

    asyncio.run(_run())
    assert telegram.closed is True
    assert signal.getsignal(sig) == previous


def test_shutdown_waits_for_running_job(schema: None, monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def short_job() -> None:
        started.set()
        await asyncio.sleep(0.3)

    monkeypatch.setattr(
        main_module,
        "build_jobs",
        lambda s: [JobSpec("short", short_job, IntervalTrigger(hours=1), run_at_startup=True)],
    )
    services = Services(settings=_settings())

    async def _run() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(main_module.run(services.settings, stop=stop, services=services))
        await asyncio.wait_for(started.wait(), 5)
        stop.set()  # the job is mid-run; shutdown must let it finish
        await task

    asyncio.run(_run())

    async def _check() -> None:
        run = await _latest_run("short")
        assert run is not None and run.status is JobRunStatus.SUCCESS

    asyncio.run(_check())


def test_shutdown_cancels_job_exceeding_grace_and_records_it(
    schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def slow_job() -> None:
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(main_module, "JOB_SHUTDOWN_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(
        main_module,
        "build_jobs",
        lambda s: [JobSpec("slow", slow_job, IntervalTrigger(hours=1), run_at_startup=True)],
    )
    telegram = FakeTelegram()
    services = Services(settings=_settings(), telegram=telegram)  # type: ignore[arg-type]

    async def _run() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(main_module.run(services.settings, stop=stop, services=services))
        await asyncio.wait_for(started.wait(), 5)
        stop.set()
        await asyncio.wait_for(task, 5)
        assert _other_tasks() == []

    asyncio.run(_run())
    assert telegram.sent == []  # a shutdown is not a failure worth an alert

    async def _check() -> None:
        run = await _latest_run("slow")
        assert run is not None
        assert run.status is JobRunStatus.FAILED
        assert run.error == CANCELLED_ERROR

    asyncio.run(_check())


def test_startup_fails_when_database_not_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'empty.sqlite3').as_posix()}"
    settings = Settings(_env_file=None, database_url=url)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    telegram = FakeTelegram()
    monkeypatch.setattr(
        main_module, "build_services", lambda s: Services(settings=s, telegram=telegram)
    )

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()
    assert exc_info.value.code == 1
    assert telegram.closed is True


def test_invalid_configuration_exits_with_code_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_module, "get_settings", lambda: Settings(_env_file=None, log_level="LOUD")
    )
    with pytest.raises(SystemExit) as exc_info:
        main_module.main()
    assert exc_info.value.code == 2


def test_gmail_job_never_starts_oauth_browser_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ForbiddenFlow:
        def __getattr__(self, name):
            raise AssertionError("OAuth browser flow must not run in production")

        @classmethod
        def from_client_secrets_file(cls, *args, **kwargs):
            raise AssertionError("OAuth browser flow must not run in production")

    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", ForbiddenFlow)
    settings = _settings(
        gmail_client_secret_file=str(tmp_path / "client_secret.json"),
        gmail_token_file=str(tmp_path / "missing_token.json"),
    )
    with pytest.raises(GmailAuthError):
        asyncio.run(gmail_import(Services(settings=settings)))


def test_broken_gmail_token_fails_job_but_not_the_process(
    schema: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module, "build_jobs", lambda s: build_jobs(s, now=NIGHT))
    telegram = FakeTelegram()
    settings = _settings(
        gmail_client_secret_file=str(tmp_path / "client_secret.json"),
        gmail_token_file=str(tmp_path / "missing_token.json"),
    )
    services = Services(settings=settings, telegram=telegram)  # type: ignore[arg-type]

    async def _run() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(main_module.run(settings, stop=stop, services=services))
        await _wait_for(lambda: any("gmail_import" in text for _, text in telegram.sent))
        assert not task.done()
        stop.set()
        await task

    asyncio.run(_run())

    async def _check() -> None:
        run = await _latest_run("gmail_import")
        assert run is not None and run.status is JobRunStatus.FAILED
        assert "GmailAuthError" in run.error

    asyncio.run(_check())


# --- Telegram polling -------------------------------------------------------------


def _poll(telegram: FakeTelegram, until, **kwargs) -> None:
    async def _run() -> None:
        task = asyncio.create_task(
            bot.run_polling(
                telegram, chat_id=CHAT_ID, job_names=["health_check"], retry_initial=0.01, **kwargs
            )
        )
        await _wait_for(lambda: until() or task.done())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())


def test_polling_answers_owner_and_ignores_foreign_chat() -> None:
    telegram = FakeTelegram([[_update(5, "/help", chat_id=999), _update(6, "/start")]])
    _poll(telegram, lambda: telegram.sent)
    assert telegram.sent == [(CHAT_ID, bot.HELP_TEXT)]
    _ = telegram.offsets  # offsets acknowledged below


def test_polling_acknowledges_updates_with_offset() -> None:
    telegram = FakeTelegram([[_update(10, "hi")], [_update(11, "hi")]])
    _poll(telegram, lambda: len(telegram.offsets) >= 3)
    assert telegram.offsets[:3] == [None, 11, 12]


def test_polling_retries_transient_errors() -> None:
    telegram = FakeTelegram(
        [TelegramAPIError(description="bad gateway", http_status=502), [_update(1, "/help")]]
    )
    _poll(telegram, lambda: telegram.sent)
    assert telegram.sent == [(CHAT_ID, bot.HELP_TEXT)]


def test_polling_stops_on_invalid_token() -> None:
    telegram = FakeTelegram([TelegramAPIError(description="Unauthorized", error_code=401)])

    async def _run() -> None:
        await asyncio.wait_for(
            bot.run_polling(telegram, chat_id=CHAT_ID, job_names=[], retry_initial=0.01), 5
        )

    asyncio.run(_run())
    assert telegram.offsets == [None]


def test_polling_survives_failing_reply() -> None:
    telegram = FakeTelegram([[_update(1, "/help")], [_update(2, "/help")]], fail_send=True)
    _poll(telegram, lambda: len(telegram.offsets) >= 3)
    assert telegram.offsets[:3] == [None, 2, 3]


def test_free_text_gets_unsupported_reply() -> None:
    assert asyncio.run(bot.answer("Сколько я трачу на подписки?", [])) == bot.UNSUPPORTED_TEXT
    assert asyncio.run(bot.answer("   ", [])) == bot.UNSUPPORTED_TEXT


def test_status_command_reports_job_runs(schema: None) -> None:
    async def _run() -> str:
        await _execute_job("health_check", _noop)
        return await bot.answer("/status", ["health_check", "gmail_import"])

    text = asyncio.run(_run())
    assert "health_check: success" in text
    assert "gmail_import: ещё не запускалась" in text


async def _noop() -> None:
    return None


# --- daily reminders --------------------------------------------------------------


async def _create_event(**kwargs) -> int:
    async with db.get_session() as session:
        event = await events.create_event(session, **kwargs)
        await session.commit()
        return event.id


async def _reminders_of(event_id: int) -> list[Reminder]:
    async with db.get_session() as session:
        return await reminders.get_reminders_for_event(session, event_id)


async def _event(event_id: int) -> Event:
    async with db.get_session() as session:
        return await session.get(Event, event_id)


async def _notifications() -> list[Notification]:
    async with db.get_session() as session:
        return list((await session.execute(select(Notification).order_by(Notification.id))).scalars())


def test_daily_reminders_queues_once_and_worker_delivers(schema: None) -> None:
    today = date(2026, 10, 3)
    telegram = FakeTelegram()
    services = Services(settings=_settings(), telegram=telegram)  # type: ignore[arg-type]

    async def _run() -> None:
        event_id = await _create_event(name="Встреча", next_date=today, action_text="Купить торт")
        await daily_reminders(services, today=today)
        await daily_reminders(services, today=today)
        stored = await _reminders_of(event_id)
        assert len(stored) == 1 and stored[0].is_sent is True  # handed to the outbox
        [queued] = await _notifications()
        assert queued.dedup_key == f"event-reminder:{stored[0].id}"
        assert telegram.sent == []  # daily_reminders itself never talks to Telegram
        await deliver_notifications(services)
        await deliver_notifications(services)

    asyncio.run(_run())
    assert len(telegram.sent) == 1
    assert "Встреча" in telegram.sent[0][1] and "Купить торт" in telegram.sent[0][1]


def test_daily_reminders_survive_telegram_outage(schema: None) -> None:
    today = date(2026, 10, 3)
    telegram = FakeTelegram(fail_send=True)
    services = Services(settings=_settings(), telegram=telegram)  # type: ignore[arg-type]

    async def _run() -> None:
        past = today - timedelta(days=2)
        event_id = await _create_event(name="ДР", next_date=past, recurrence="yearly")
        async with db.get_session() as session:
            created = await reminders.generate_reminders(session, await session.get(Event, event_id))
            for reminder in created:  # created in time, then missed while the app was down
                reminder.created_at = datetime(2026, 9, 1)
            await session.commit()
        await daily_reminders(services, today=today)  # no Telegram needed to queue
        await deliver_notifications(services)
        [queued] = await _notifications()
        assert queued.status.value == "pending" and queued.last_error
        # The event advances; the queued reminder is unaffected by that.
        assert (await _event(event_id)).next_date == date(2027, 10, 1)

    asyncio.run(_run())


def test_daily_reminders_advances_yearly_event_after_queueing(schema: None) -> None:
    today = date(2026, 10, 3)
    services = Services(settings=_settings())

    async def _run() -> None:
        event_id = await _create_event(
            name="Годовщина", next_date=date(2026, 10, 1), recurrence="yearly"
        )
        async with db.get_session() as session:
            created = await reminders.generate_reminders(session, await session.get(Event, event_id))
            for reminder in created:  # created in time, then missed while the app was down
                reminder.created_at = datetime(2026, 9, 1)
            await session.commit()
        await daily_reminders(services, today=today)
        assert (await _event(event_id)).next_date == date(2027, 10, 1)
        # The missed reminder was queued before advancing regenerated reminders.
        [queued] = await _notifications()
        assert "Годовщина" in queued.text

    asyncio.run(_run())


def test_daily_reminders_skips_inactive_events(schema: None) -> None:
    today = date(2026, 10, 3)
    services = Services(settings=_settings())

    async def _run() -> None:
        event_id = await _create_event(name="Off", next_date=today)
        async with db.get_session() as session:
            event = await session.get(Event, event_id)
            await reminders.generate_reminders(session, event)
            event.is_active = False
            await session.commit()
        await daily_reminders(services, today=today)
        assert await _notifications() == []

    asyncio.run(_run())


def test_daily_reminders_imports_sheet_edits_before_exporting(schema: None) -> None:
    today = date(2026, 10, 3)

    async def _run() -> None:
        event_id = await _create_event(name="Old name", next_date=date(2026, 12, 1))
        row = event_to_row(await _event(event_id))
        row[1] = "Edited in sheet"
        sheets = FakeSheets({"Events": [list(EVENT_HEADERS), row]})
        services = Services(settings=_settings(), sheets=sheets)  # type: ignore[arg-type]
        await daily_reminders(services, today=today)
        assert (await _event(event_id)).name == "Edited in sheet"
        assert sheets.sheets["Events"][1][1] == "Edited in sheet"

    asyncio.run(_run())


def test_sheets_sync_keeps_invalid_rows_untouched(schema: None) -> None:
    sheets = FakeSheets({"Events": [list(EVENT_HEADERS), ["not-an-id", "x"]]})
    services = Services(settings=_settings(), sheets=sheets)  # type: ignore[arg-type]

    with pytest.raises(SheetsSyncError, match="Events"):
        asyncio.run(sheets_sync(services))
    assert sheets.sheets["Events"][1][:2] == ["not-an-id", "x"]  # user's row preserved
    assert not any(sheets.sheets["Events"][1][2:])  # only padded with empty cells


# --- failure alerts -----------------------------------------------------------------


def test_failure_alert_sent_once_per_failure_streak(schema: None) -> None:
    telegram = FakeTelegram()
    notifier = make_streak_failure_notifier(telegram, CHAT_ID)  # type: ignore[arg-type]

    async def boom() -> None:
        raise RuntimeError("boom")

    async def _run() -> None:
        await _execute_job("flaky", boom, notifier=notifier)
        await _execute_job("flaky", boom, notifier=notifier)
        await _execute_job("flaky", _noop, notifier=notifier)
        await _execute_job("flaky", boom, notifier=notifier)
        async with db.get_session() as session:
            statuses = [
                r.status
                for r in (await session.execute(select(JobRun).order_by(JobRun.id))).scalars()
            ]
        assert statuses == [
            JobRunStatus.FAILED,
            JobRunStatus.FAILED,
            JobRunStatus.SUCCESS,
            JobRunStatus.FAILED,
        ]

    asyncio.run(_run())
    assert len(telegram.sent) == 2  # first failure, and the first after recovery
