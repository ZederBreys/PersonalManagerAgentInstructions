"""Sheet UX: tabs found by hidden marker, renamable headers, bot-filled columns,
row feedback (colour + note on the ID cell), the delete word, and the cheap
change watcher. Dates are in the year 2999 so results never depend on today."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select

import app.main as main_module
from app import db, job_runs
from app.config import Settings
from app.google_sheets.client import GoogleSheetsClient, GoogleSheetsError
from app.google_sheets.feedback import GREEN, RED, build_requests, event_note, expense_note
from app.google_sheets.mappers import EVENT_HEADERS, EXPENSE_HEADERS
from app.google_sheets.setup import ROLE_EVENTS, ensure_workbook
from app.jobs import Services, SheetsSyncError, build_jobs, daily_reminders, sheets_poll, sheets_sync
from app.logging_setup import setup_logging
from app.models.event import Event, EventRecurrence
from app.models.expense import ExpensePeriod, RecurringExpense
from app.models.job_run import JobRun, JobRunStatus
from app.models.reminder import Reminder
from app.scheduler import JobSpec, _execute_job, create_scheduler
from tests.test_google_sheets_concurrency import ThreadUnsafeService
from tests.test_google_sheets_two_way import EVENTS, EXPENSES, SheetStore, _records, _store, _sync

EVENT_ROW = ["", "Годовщина", "24.08.2999", "none", "за 7 дней", "Купить подарок", ""]
EXPENSE_ROW = ["", "Интернет", "1 000,50", "RUB", "monthly", "15", "Дом", "15.10.2999", "да", "3"]


def _services(store, **kwargs) -> Services:
    return Services(settings=Settings(_env_file=None), sheets=store, **kwargs)  # type: ignore[arg-type]


def _run(coro):
    return asyncio.run(coro)


def _sync_ignoring_row_errors(store) -> None:
    try:
        _sync(store)
    except SheetsSyncError:
        pass


# --- tabs are found by a hidden marker, not by name or position ---------------------------


def test_legacy_tabs_are_adopted_and_missing_ones_created(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    assert set(store.sheets) == {"Events", "Expenses", "Reminders", "Settings", "Inbox", "Email"}
    assert sorted(store.roles.values()) == sorted(["events", "expenses", "reminders", "settings", "inbox", "email"])
    assert store.sheet_id("Events") in store.roles  # the existing tab was adopted, not replaced


def test_renamed_and_reordered_tabs_are_followed(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    store.rename("Events", "Напоминания")
    store.rename("Expenses", "Расходники")
    store.sheets = dict(reversed(list(store.sheets.items())))  # tabs reordered
    store.sheets["Напоминания"].append(["", "Второе", "25.08.2999", "none", "0", "", ""])

    _sync(store)

    assert [e.name for e in _records(EVENTS)] == ["Годовщина", "Второе"]
    assert "Events" not in store.sheets and "Expenses" not in store.sheets  # nothing re-created
    assert store.data_rows("Напоминания")[1][0] == "2"  # the new row got its ID


def test_deleted_tab_is_recreated_with_headers(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    del store.sheets["Reminders"]
    _sync(store)
    assert store.sheets["Reminders"][0][0] == "ID"


def test_default_title_taken_by_another_role_gets_a_free_title(schema: None) -> None:
    # The user swapped the names: the tab called "Events" holds the expenses.
    store = SheetStore({"Events": [list(EXPENSE_HEADERS)], "Мои платежи": [list(EVENT_HEADERS)]})
    store.roles = {store.sheet_id("Events"): "expenses", store.sheet_id("Мои платежи"): "events"}
    _sync(store)
    assert store.roles[store.sheet_id("Events")] == "expenses"  # untouched
    assert "Events 2" not in store.sheets  # events role was found by its marker


def test_unmarked_default_title_conflict_creates_a_numbered_tab(schema: None) -> None:
    store = SheetStore({"Events": [list(EXPENSE_HEADERS)]})
    store.roles = {store.sheet_id("Events"): "expenses"}  # "Events" belongs to another role
    _sync(store)
    assert "Events 2" in store.sheets and "events" in store.roles.values()


# --- headers belong to the user ---------------------------------------------------------------


def test_user_headers_are_kept_and_missing_ones_added(schema: None) -> None:
    custom = ["Идентификатор", "Напоминание", "Дата", "Повтор", "Напомнить за", "Что сделать", "Статус"]
    store = SheetStore({"Events": [custom, ["", "Тест", "24.12.2999", "none", "1", "", ""]]})
    _sync(store)
    header = store.sheets["Events"][0]
    assert header[:6] == custom[:6]  # the user's own words are never overwritten
    assert header[6] == "Активно"  # the old default "Статус" is replaced once
    assert header[7:9] == ["Когда будет напомнено", "Когда событие случится"]  # new bot columns
    assert [e.name for e in _records(EVENTS)] == ["Тест"]  # columns are by position: renamed A1 is fine


def test_renamed_header_survives_every_sync(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    store.sheets["Events"][0][1] = "Моё название"
    store.sheets["Events"][0][6] = "Статус"  # a custom word the user chose again
    _sync(store)
    _sync(store)
    assert store.sheets["Events"][0][1] == "Моё название"
    assert store.sheets["Events"][0][6] == "Активно"  # only the exact legacy default is upgraded


def test_custom_g_header_is_not_touched(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    store.sheets["Events"][0][6] = "Включено"
    _sync(store)
    assert store.sheets["Events"][0][6] == "Включено"


# --- the two columns filled by the bot ------------------------------------------------------


def test_bot_filled_columns_show_when_it_happens_and_when_it_is_reminded(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)  # 24.08.2999, one-off, remind 7 days before
    _sync(store)
    row = store.sheets["Events"][1]
    assert row[7] == "17.08.2999" and row[8] == "24.08.2999"


def test_bot_columns_are_ignored_on_import(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    store.sheets["Events"][1][7] = "01.01.2001"  # the user types over a bot column
    store.sheets["Events"][1][8] = "garbage"
    _sync(store)
    assert store.sheets["Events"][1][7] == "17.08.2999"  # the bot restores its own value
    assert store.sheets["Events"][1][8] == "24.08.2999"
    assert _records(EVENTS)[0].next_date == date(2999, 8, 24)


# --- feedback on the ID cell ---------------------------------------------------------------------


def test_accepted_row_gets_green_id_with_the_bots_view(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    assert store.colour("Events", 2) == GREEN
    note = store.note("Events", 2)
    assert note.startswith("✓ Принято") and "МСК" in note
    assert "Событие: Годовщина" in note and "Повтор: один раз" in note
    assert "Когда случится: 24.08.2999" in note
    assert "за 7 дн. → 17.08.2999" in note  # computed by the bot, not echoed
    assert "Что сделать: Купить подарок" in note and "Активно: да" in note


def test_expense_note_shows_amount_period_and_reminder_date(schema: None) -> None:
    store = _store(EXPENSES, EXPENSE_ROW)
    _sync(store)
    assert store.colour("Expenses", 2) == GREEN
    note = store.note("Expenses", 2)
    assert "Платёж: Интернет — 1000.50 RUB" in note
    assert "Период: ежемесячно · день оплаты: 15" in note
    assert "Следующая оплата: 15.10.2999" in note
    assert "Напоминание: за 3 дн. → 12.10.2999" in note and "Категория: Дом" in note


def test_rejected_row_gets_red_id_with_the_reason_then_turns_green_when_fixed(schema: None) -> None:
    bad = ["", "Плохое", "24.08.2999", "1", "", "", ""]
    store = _store(EVENTS, bad, EVENT_ROW)
    _sync_ignoring_row_errors(store)

    assert store.colour("Events", 2) == RED
    note = store.note("Events", 2)
    assert note.startswith("✗ Строка не принята") and "Invalid recurrence: '1'" in note and "'none'" in note
    assert store.colour("Events", 3) == GREEN  # the neighbour is accepted anyway
    assert store.sheets["Events"][1][:4] == ["", "Плохое", "24.08.2999", "1"]  # kept as typed

    store.sheets["Events"][1][3] = "none"  # the user fixes the cell
    _sync(store)
    assert store.colour("Events", 2) == GREEN
    assert store.note("Events", 2).startswith("✓ Принято")


def test_paused_event_note_says_so(schema: None) -> None:
    row = list(EVENT_ROW)
    row[6] = "нет"
    store = _store(EVENTS, row)
    _sync(store)
    note = store.note("Events", 2)
    assert "Активно: нет — на паузе, напоминаний не будет" in note
    assert store.sheets["Events"][1][7:9] == ["", ""]


def test_note_failure_does_not_fail_the_sync(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    store.fail_batch_update = True
    _sync(store)  # must not raise
    assert [e.name for e in _records(EVENTS)] == ["Годовщина"]


def test_event_note_for_a_yearly_event_looks_ahead() -> None:
    event = Event(
        id=1, name="ДР", next_date=date(2026, 8, 24), anchor_date=date(2026, 8, 24),
        recurrence=EventRecurrence.YEARLY, reminder_offsets=[7, 0], is_active=True, action_text=None,
    )
    now = datetime(2026, 10, 3, 16, 42, tzinfo=timezone.utc)
    note = event_note(event, today=date(2026, 10, 3), now=now)
    assert "03.10.2026 19:42 МСК" in note  # Moscow time, UTC+3
    assert "Повтор: каждый год" in note and "Когда случится: 24.08.2027" in note
    assert "за 7 дн. → 17.08.2027; в день события → 24.08.2027" in note
    assert "Ближайшее напоминание: 17.08.2027" in note


def test_expense_note_text_directly() -> None:
    expense = RecurringExpense(
        id=1, name="Netflix", amount_minor=1299, currency="USD", period=ExpensePeriod.YEARLY,
        payment_day=1, category=None, next_payment_date=date(2026, 11, 1), is_active=False,
        reminder_days_before=0,
    )
    note = expense_note(expense, today=date(2026, 10, 3))
    assert "Netflix — 12.99 USD" in note and "ежегодно" in note
    assert "за 0 дн. → 01.11.2026" in note and "Активен: нет — на паузе" in note


def test_feedback_requests_have_the_shape_the_api_expects(schema: None) -> None:
    store = _store(EVENTS, ["", "Плохое", "x", "", "", "", ""], EVENT_ROW)
    _sync_ignoring_row_errors(store)
    sheet_id = store.sheet_id("Events")
    requests = [r for r in store.batch_requests if "updateCells" in r]
    by_row = {r["updateCells"]["range"]["startRowIndex"]: r["updateCells"] for r in requests}
    assert set(by_row) == {1, 2}  # 0-based rows of sheet rows 2 and 3
    for update in by_row.values():
        assert update["range"]["sheetId"] == sheet_id
        assert (update["range"]["startColumnIndex"], update["range"]["endColumnIndex"]) == (0, 1)
        assert update["fields"] == "note,userEnteredFormat.backgroundColor"
    assert by_row[1]["rows"][0]["values"][0]["userEnteredFormat"]["backgroundColor"] == RED
    assert by_row[2]["rows"][0]["values"][0]["userEnteredFormat"]["backgroundColor"] == GREEN


# --- the delete word -------------------------------------------------------------------------------


def _count(model: type) -> int:
    async def _r() -> int:
        async with db.get_session() as session:
            return await session.scalar(select(func.count()).select_from(model))

    return _run(_r())


@pytest.mark.parametrize("word", ["удалить", "Удалить", "УДАЛИТЬ", "  удалить  ", "УдАлИтЬ"])
def test_delete_word_in_any_case_deletes_the_event_and_blanks_the_row(schema: None, word: str) -> None:
    keep = ["", "Останется", "25.08.2999", "none", "0", "", ""]
    store = _store(EVENTS, EVENT_ROW, keep)
    _sync(store)
    assert _count(Event) == 2 and _count(Reminder) == 2
    store.sheets["Events"][1][6] = word

    _sync(store)

    assert [e.name for e in _records(EVENTS)] == ["Останется"]
    assert _count(Reminder) == 1  # its reminders went with it
    assert not any(str(c).strip() for c in store.sheets["Events"][1])  # the row is blank
    assert store.note("Events", 2) is None and store.colour("Events", 2) is None
    assert store.colour("Events", 3) == GREEN  # the next row is untouched
    _sync(store)  # nothing comes back
    assert _count(Event) == 1


def test_delete_word_deletes_an_expense(schema: None) -> None:
    store = _store(EXPENSES, EXPENSE_ROW)
    _sync(store)
    store.sheets["Expenses"][1][8] = "Удалить"
    _sync(store)
    assert _count(RecurringExpense) == 0
    assert not any(str(c).strip() for c in store.sheets["Expenses"][1])


def test_delete_word_ignores_the_rest_of_the_row(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    store.sheets["Events"][1][2] = "not a date"  # would be an error for an edit
    store.sheets["Events"][1][6] = "удалить"
    _sync(store)
    assert _count(Event) == 0


def test_delete_word_on_a_row_without_a_record_is_an_error(schema: None) -> None:
    store = _store(EVENTS, ["", "Ещё не сохранено", "24.08.2999", "none", "0", "", "удалить"])
    _sync_ignoring_row_errors(store)
    assert _count(Event) == 0
    assert store.colour("Events", 2) == RED and "Nothing to delete" in store.note("Events", 2)


def test_delete_word_for_an_unknown_id_is_an_error(schema: None) -> None:
    store = _store(EVENTS, ["999", "x", "", "", "", "", "удалить"])
    _sync_ignoring_row_errors(store)
    assert store.colour("Events", 2) == RED and "Unknown event ID: 999" in store.note("Events", 2)


def test_plain_no_does_not_delete(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    _sync(store)
    store.sheets["Events"][1][6] = "нет"
    _sync(store)
    assert _count(Event) == 1 and _records(EVENTS)[0].is_active is False


# --- the cheap change watcher --------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr("app.jobs.time.monotonic", fake)
    return fake


def _watched(store) -> tuple[Services, list[int]]:
    services = _services(store)
    requested: list[int] = []
    services.request_sync = lambda: requested.append(1)
    _run(sheets_sync(services))  # the baseline: what the sheet looks like after a sync
    return services, requested


def _poll(services: Services, times: int = 1) -> None:
    for _ in range(times):
        _run(sheets_poll(services))


def test_poll_does_nothing_while_the_sheet_is_unchanged(schema: None, clock: _Clock) -> None:
    store = _store(EVENTS, EVENT_ROW)
    services, requested = _watched(store)
    store.requests.update(read=0, write=0)
    _poll(services, 5)
    assert requested == []
    assert store.requests == {"read": 5, "write": 0}  # one read per poll, never a write


def test_poll_requests_a_sync_only_after_edits_settle(schema: None, clock: _Clock) -> None:
    store = _store(EVENTS, EVENT_ROW)
    services, requested = _watched(store)

    store.sheets["Events"][1][1] = "Новое имя"
    _poll(services)  # first sight of the change: the user may still be typing
    assert requested == []
    store.sheets["Events"][1][2] = "25.08.2999"  # still typing
    _poll(services)
    assert requested == []
    _poll(services)  # unchanged since the previous poll -> settled
    assert requested == [1]
    _poll(services)  # the cooldown prevents a second request while the sync is pending
    assert requested == [1]


def test_after_the_sync_the_new_state_is_the_baseline(schema: None, clock: _Clock) -> None:
    store = _store(EVENTS, EVENT_ROW)
    services, requested = _watched(store)
    store.sheets["Events"][1][1] = "Новое имя"
    _poll(services, 2)
    assert requested == [1]

    _run(sheets_sync(services))  # what the scheduler does after the request
    clock.now += 60
    _poll(services, 3)
    assert requested == [1]  # the sync's own writes do not trigger another one
    assert _records(EVENTS)[0].name == "Новое имя"


def test_poll_waits_while_a_sync_holds_the_lock(schema: None, clock: _Clock) -> None:
    store = _store(EVENTS, EVENT_ROW)
    services, _ = _watched(store)
    store.requests.update(read=0, write=0)

    async def _go() -> None:
        async with services.data_lock:
            await sheets_poll(services)

    _run(_go())
    assert store.requests["read"] == 0


def test_poll_backs_off_after_an_api_error_and_follows_a_renamed_tab(schema: None, clock: _Clock) -> None:
    store = _store(EVENTS, EVENT_ROW)
    services, requested = _watched(store)

    store.rename("Events", "Напоминания")  # the cached title is now stale
    _poll(services)  # the read fails (Unable to parse range) -> back off
    assert services.sheet_layout is None and services.poll_pause_until > clock.now
    reads = store.requests["read"]
    _poll(services)
    assert store.requests["read"] == reads  # paused: no requests at all

    clock.now += 31
    store.sheets["Напоминания"][1][1] = "После переименования"
    _poll(services, 3)  # the layout is resolved again by the hidden marker
    assert services.sheet_layout[ROLE_EVENTS].title == "Напоминания"
    assert requested == [1]


def test_poll_survives_rate_limiting(schema: None, clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(EVENTS, EVENT_ROW)
    services, requested = _watched(store)

    async def limited(ranges):
        raise GoogleSheetsError(message="Quota exceeded", http_status=429)

    monkeypatch.setattr(store, "batch_get", limited)
    _poll(services, 3)  # must not raise
    assert requested == [] and services.poll_pause_until > clock.now


def test_the_watcher_writes_no_job_runs_but_real_jobs_do(schema: None) -> None:
    async def watch() -> None:
        return None

    scheduler = create_scheduler(
        jobs=[
            JobSpec("watch", watch, build_jobs(_services(None))[0].trigger, track_runs=False),
            JobSpec("work", watch, build_jobs(_services(None))[0].trigger),
        ]
    )

    async def _go() -> list[str]:
        await scheduler.get_job("watch").func()
        await scheduler.get_job("work").func()
        async with db.get_session() as session:
            return [r.job_name for r in (await session.execute(select(JobRun))).scalars()]

    assert _run(_go()) == ["work"]


def test_sheets_poll_job_is_registered_with_the_configured_interval() -> None:
    settings = Settings(_env_file=None, sheets_poll_seconds=7)
    jobs = {j.name: j for j in build_jobs(Services(settings=settings, sheets=SheetStore()))}  # type: ignore[arg-type]
    assert jobs["sheets_poll"].trigger.interval.total_seconds() == 7
    assert "sheets_sync" in jobs  # the periodic full sync stays as the safety net
    assert "sheets_poll" not in build_jobs(Services(settings=settings))  # no sheets, no watcher


def test_poll_interval_setting_is_validated() -> None:
    assert Settings(_env_file=None).sheets_poll_seconds == 5
    with pytest.raises(ValueError):
        Settings(_env_file=None, sheets_poll_seconds=0)


# --- alerts and logging ---------------------------------------------------------------------------------


def test_row_errors_are_recorded_but_do_not_alert(schema: None) -> None:
    alerts: list[str] = []

    async def notifier(session, run) -> None:
        alerts.append(run.error)

    async def bad_rows() -> None:
        raise SheetsSyncError("Invalid rows were skipped")

    async def crash() -> None:
        raise RuntimeError("boom")

    async def _go() -> JobRun:
        await _execute_job("sheets_sync", bad_rows, notifier=notifier)
        async with db.get_session() as session:
            rows = list((await session.execute(select(JobRun))).scalars())
        assert [r.status for r in rows] == [JobRunStatus.FAILED]  # recorded...
        assert alerts == []  # ...but nobody is alerted about a half-typed row
        await _execute_job("other", crash, notifier=notifier)
        return rows[0]

    _run(_go())
    assert len(alerts) == 1 and "boom" in alerts[0]  # real failures still alert


def test_per_execution_scheduler_logs_are_quiet() -> None:
    setup_logging("INFO")
    assert logging.getLogger("apscheduler.executors.default").level == logging.WARNING


# --- through the real client and the real scheduler ---------------------------------------------------------


def test_real_client_follows_a_renamed_tab_and_writes_notes(schema: None) -> None:
    service = ThreadUnsafeService({"Events": [list(EVENT_HEADERS), list(EVENT_ROW)]})
    client = GoogleSheetsClient("unused.json", "spreadsheet-id", service=service)
    services = Services(settings=Settings(_env_file=None), sheets=client)

    _run(sheets_sync(services))
    service.store.rename("Events", "Напоминания")
    service.sheets["Напоминания"].append(["", "Второе", "25.08.2999", "none", "0", "", ""])
    _run(sheets_sync(services))

    assert [e.name for e in _records(EVENTS)] == ["Годовщина", "Второе"]
    assert service.overlaps == 0
    assert service.store.note("Напоминания", 3).startswith("✓ Принято")
    assert "Events" not in service.sheets  # quoted Cyrillic ranges round-trip through the client


def test_daily_reminders_also_refreshes_the_sheet_and_baseline(schema: None) -> None:
    store = _store(EVENTS, EVENT_ROW)
    services = _services(store)
    _run(daily_reminders(services, today=date(2026, 10, 3)))
    assert str(store.sheets["Events"][1][0]) == "1"  # imported, and the ID written back
    assert store.colour("Events", 2) == GREEN
    assert services.poll_applied is not None  # the watcher will not re-sync this state


def test_end_to_end_an_edit_is_synced_within_seconds_without_job_run_noise(
    schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    night = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(main_module, "build_jobs", lambda s: build_jobs(s, now=night))
    store = _store(EVENTS, EVENT_ROW)
    settings = Settings(_env_file=None, sheets_poll_seconds=1)
    services = Services(settings=settings, sheets=store)  # type: ignore[arg-type]

    async def _wait(condition, timeout: float = 15.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not await condition():
            assert asyncio.get_running_loop().time() < deadline, "timed out"
            await asyncio.sleep(0.1)

    async def event_names() -> list[str]:
        async with db.get_session() as session:
            return [e.name for e in (await session.execute(select(Event).order_by(Event.id))).scalars()]

    async def _go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(main_module.run(settings, stop=stop, services=services))
        await _wait(lambda: _is(event_names, ["Годовщина"]))  # the startup sync
        store.sheets["Events"].append(["", "Добавлено пока работает", "26.08.2999", "none", "0", "", ""])
        await _wait(lambda: _is(event_names, ["Годовщина", "Добавлено пока работает"]))
        stop.set()
        await task

    async def _is(getter, expected) -> bool:
        return await getter() == expected

    _run(_go())

    async def _runs() -> list[str]:
        async with db.get_session() as session:
            return [r.job_name for r in (await session.execute(select(JobRun))).scalars()]

    names = _run(_runs())
    assert "sheets_poll" not in names  # the watcher is not recorded
    assert names.count("sheets_sync") >= 2  # the startup sync and the one the watcher requested
