"""How people really type into the sheet, what we write back, and startup recovery.

Covers the production findings: the sheet is ru_RU (dates come back as
``DD.MM.YYYY``), writes must not turn the user's cells into text (the stray
apostrophe), the typed row from the real sheet must import, and jobs left
``running`` by a crash are closed at the very next start.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

import app.main as main_module
from app import db, job_runs
from app.config import Settings
from app.google_sheets.mappers import (
    event_to_row,
    expense_to_row,
    minor_to_number,
    parse_event_row,
    parse_expense_row,
    text_cell,
)
from app.jobs import Services, SheetsSyncError
from app.models.event import Event, EventRecurrence
from app.models.expense import ExpensePeriod, RecurringExpense
from app.models.job_run import JobRun, JobRunStatus
from tests.test_google_sheets_two_way import EVENTS, EXPENSES, _records, _store, _sync


# --- lenient but strict-where-it-matters parsing -------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("none", "none"), ("Разово", "none"), ("нет", "none"), ("Один раз", "none"),
        ("yearly", "yearly"), ("Ежегодно", "yearly"), ("каждый год", "yearly"),
        ("  Раз  в год ", "yearly"),
    ],
)
def test_recurrence_accepts_natural_wording(typed: str, expected: str) -> None:
    assert parse_event_row([1, "x", "2026-10-15", typed])["recurrence"].value == expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("monthly", ExpensePeriod.MONTHLY), ("Ежемесячно", ExpensePeriod.MONTHLY),
        ("раз в месяц", ExpensePeriod.MONTHLY), ("Ежеквартально", ExpensePeriod.QUARTERLY),
        ("квартал", ExpensePeriod.QUARTERLY), ("раз в год", ExpensePeriod.YEARLY),
        ("Ежегодно", ExpensePeriod.YEARLY),
    ],
)
def test_period_accepts_natural_wording(typed: str, expected: ExpensePeriod) -> None:
    assert parse_expense_row([1, "x", "1", "RUB", typed])["period"] is expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("да", True), ("Да", True), ("ИСТИНА", True), ("TRUE", True), ("1", True), ("+", True),
     ("нет", False), ("Нет", False), ("ЛОЖЬ", False), ("FALSE", False), ("0", False), ("-", False)],
)
def test_yes_no_accepts_text_and_checkbox_forms(typed: str, expected: bool) -> None:
    assert parse_event_row([1, "x", "", "", "", "", typed])["is_active"] is expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("₽", "RUB"), ("руб.", "RUB"), ("Рублей", "RUB"), ("$", "USD"), ("€", "EUR"), ("евро", "EUR"),
     ("usd", "usd"), ("RUB", "RUB")],  # codes are kept (upper-cased by the domain layer)
)
def test_currency_symbols_and_words(typed: str, expected: str) -> None:
    assert parse_expense_row([1, "x", "1", typed])["currency"] == expected


@pytest.mark.parametrize(
    ("typed", "minor"),
    [("1 000,50", 100050), ("1 000,50", 100050), ("12,5", 1250), ("1000", 100000)],
)
def test_amount_with_spaces_and_decimal_comma(typed: str, minor: int) -> None:
    assert parse_expense_row([1, "x", typed])["amount_minor"] == minor


def test_ambiguous_values_are_still_rejected_with_helpful_message() -> None:
    with pytest.raises(ValueError, match="none.*yearly"):
        parse_event_row([1, "x", "2026-10-15", "1"])  # what the test row had
    with pytest.raises(ValueError, match="monthly.*quarterly.*yearly"):
        parse_expense_row([1, "x", "1", "RUB", "иногда"])
    with pytest.raises(ValueError, match="да.*нет"):
        parse_event_row([1, "x", "", "", "", "", "возможно"])
    with pytest.raises(ValueError):
        parse_expense_row([1, "x", "1,234.56"])


# --- what we write back --------------------------------------------------------------


def test_text_cell_protects_only_what_sheets_would_misread() -> None:
    assert text_cell("Тест") == "Тест"
    assert text_cell("Rent 2026") == "Rent 2026"
    assert text_cell("") == ""
    for risky in ["2026", "1/2", "12:30", "=SUM(A1)", "+7 999", "-5", "@user", " leading space"]:
        assert text_cell(risky) == "'" + risky


def test_exported_numbers_are_numbers_and_text_is_protected() -> None:
    assert minor_to_number(500) == 5 and isinstance(minor_to_number(500), int)
    assert minor_to_number(1299) == 12.99 and isinstance(minor_to_number(1299), float)
    expense = RecurringExpense(
        id=1, name="2026", amount_minor=100050, currency="RUB", period=ExpensePeriod.MONTHLY,
        payment_day=15, category="=1+1", next_payment_date=date(2026, 10, 15), is_active=True,
        reminder_days_before=3,
    )
    row = expense_to_row(expense)
    assert row[1] == "'2026" and row[6] == "'=1+1"
    assert row[2] == 1000.5 and isinstance(row[5], int)
    event = Event(id=2, name="1/2", next_date=date(2026, 1, 2), reminder_offsets=[0], action_text="=x",
                  recurrence=EventRecurrence.NONE, is_active=True)
    assert event_to_row(event)[1] == "'1/2" and event_to_row(event)[5] == "'=x"


def test_numeric_looking_name_survives_the_round_trip_as_text(schema: None) -> None:
    row = list(EVENTS.new_row)
    row[1] = "2026"  # typed as a number by the user -> arrives as "2026"
    store = _store(EVENTS, row)
    _sync(store)
    _sync(store)
    [event] = _records(EVENTS)
    assert event.name == "2026"
    assert store.data_rows("Events")[0][1] == "2026"  # the apostrophe is not stored
    assert len(_records(EVENTS)) == 1


# --- the row from the real sheet ---------------------------------------------------------


def test_row_typed_in_the_real_sheet_is_imported(schema: None) -> None:
    # Exactly what the production sheet held: ru date, "none", reminder "1", note "1".
    store = _store(EVENTS, ["", "Тест", "24.08.2025", "none", "1", "1", ""])
    _sync(store)
    [event] = _records(EVENTS)
    assert (event.name, event.next_date) == ("Тест", date(2025, 8, 24))
    assert event.recurrence.value == "none" and event.reminder_offsets == [1]
    assert event.action_text == "1" and event.is_active is True
    assert store.data_rows("Events")[0][0] == str(event.id)


def test_natural_wording_row_is_imported_end_to_end(schema: None) -> None:
    _sync(_store(EXPENSES, ["", "Интернет", "1 000,50", "₽", "Раз в месяц", "15", "Дом", "15.10.2026", "Да", "3"]))
    [expense] = _records(EXPENSES)
    assert expense.amount_minor == 100050 and expense.currency == "RUB"
    assert expense.period is ExpensePeriod.MONTHLY and expense.next_payment_date == date(2026, 10, 15)


def test_alert_message_lists_the_rows_and_reasons(schema: None) -> None:
    store = _store(EVENTS, ["", "Тест", "24.08.2025", "1", "", "", ""], ["", "Плохая", "вчера"])
    with pytest.raises(SheetsSyncError) as exc_info:
        _sync(store)
    message = str(exc_info.value)
    assert "Events: Row 2: Invalid recurrence: '1'" in message and "'none'" in message
    assert "Events: Row 3:" in message and "Invalid date: 'вчера'" in message
    assert "left as typed" in message


# --- startup recovery of jobs killed by a crash ---------------------------------------------


class _Telegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str):
        self.sent.append(text)
        return {"message_id": len(self.sent)}

    async def aclose(self) -> None:
        return None


def _start_app_after_crash(exclusive: bool) -> tuple[JobRun, _Telegram]:
    settings = Settings(_env_file=None, telegram_chat_id=1)
    telegram = _Telegram()
    services = Services(settings=settings, telegram=telegram)  # type: ignore[arg-type]

    async def _go() -> JobRun:
        async with db.get_session() as session:
            run = await job_runs.create_job_run(session, job_name="crashed_job")
            await job_runs.start_job_run(session, run)  # started just now, never finished
            await session.commit()
            run_id = run.id
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.3, stop.set)
        await main_module.run(settings, stop=stop, services=services, exclusive=exclusive)
        async with db.get_session() as session:
            return await session.get(JobRun, run_id)

    return asyncio.run(_go()), telegram


def test_jobs_left_running_by_a_crash_are_closed_at_once_when_exclusive(schema: None) -> None:
    run, telegram = _start_app_after_crash(exclusive=True)
    assert run.status is JobRunStatus.FAILED and run.finished_at is not None
    assert any("crashed_job" in text and "прервана" in text for text in telegram.sent)


def test_fresh_running_job_is_kept_without_the_instance_lock(schema: None) -> None:
    # Without the single-instance guarantee another live process may own the job.
    run, telegram = _start_app_after_crash(exclusive=False)
    assert run.status is JobRunStatus.RUNNING
    assert not any("crashed_job" in text for text in telegram.sent)


# --- phrases people write in "Повтор" and "Напоминание" ---------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("Один раз", "none"), ("Единожды", "none"), ("Не повторяется", "none"),
        ("Через год", "yearly"), ("Раз в год", "yearly"),
    ],
)
def test_recurrence_phrases(typed: str, expected: str) -> None:
    assert parse_event_row([1, "x", "2026-10-15", typed])["recurrence"].value == expected


@pytest.mark.parametrize("typed", ["Каждый 2 месяца", "Каждые 2 месяца", "каждый месяц", "раз в неделю", "ежедневно"])
def test_unsupported_repeat_intervals_get_a_specific_message(typed: str) -> None:
    with pytest.raises(ValueError, match="Unsupported recurrence.*only.*'none'.*'yearly'.*Expenses"):
        parse_event_row([1, "x", "2026-10-15", typed])


@pytest.mark.parametrize(
    ("typed", "days"),
    [
        ("За 15 дней", [15]), ("за 3 дня", [3]), ("За день до события", [1]),
        ("за 1 день", [1]), ("в день события", [0]), ("в день", [0]),
        ("за неделю", [7]), ("за 2 недели", [14]), ("за 3 д.", [3]),
        ("за 3 дня до даты", [3]), ("7", [7]), ("0, 15", [0, 15]),
        ("за 15 дней и за 3 дня", [3, 15]), ("за 15 дней, за день до события, в день события", [0, 1, 15]),
    ],
)
def test_reminder_phrases(typed: str, days: list[int]) -> None:
    assert parse_event_row([1, "x", "2026-10-15", "", typed])["reminder_offsets"] == days


@pytest.mark.parametrize("typed", ["скоро", "за -3 дня", "за дней", "-1", "a,b", "1,,2", "за три дня"])
def test_unclear_reminder_text_is_rejected_with_examples(typed: str) -> None:
    with pytest.raises(ValueError, match="Invalid reminder offsets.*за 15 дней"):
        parse_event_row([1, "x", "2026-10-15", "", typed])


def test_expense_reminder_days_accept_phrases_but_only_one_value() -> None:
    assert parse_expense_row([1, "x", "1", "RUB", "", "", "", "", "", "за 5 дней"])["reminder_days_before"] == 5
    assert parse_expense_row([1, "x", "1", "RUB", "", "", "", "", "", "0"])["reminder_days_before"] == 0
    with pytest.raises(ValueError, match="single value"):
        parse_expense_row([1, "x", "1", "RUB", "", "", "", "", "", "за 5 дней и за 1 день"])


def test_event_row_with_phrases_is_imported_end_to_end(schema: None) -> None:
    _sync(_store(EVENTS, ["", "Годовщина", "24.12.2026", "Через год", "за 15 дней, за день до события", "Купить подарок", "Да"]))
    [event] = _records(EVENTS)
    assert event.recurrence.value == "yearly" and event.reminder_offsets == [1, 15]
    assert event.is_active is True


# --- notes to the right of the table ------------------------------------------------------


def test_notes_right_of_the_table_stay_next_to_their_record(schema: None) -> None:
    """A blank row between records used to be compacted: the rows below shifted
    up while the user's notes (column H and beyond) stayed where they were."""

    from app.google_sheets.mappers import EVENT_HEADERS
    from tests.test_google_sheets_two_way import SheetStore

    first = ["", "Первое", "2026-12-24", "none", "0", "", "да", "", "", "заметка 1"]
    second = ["", "Второе", "2026-12-25", "none", "0", "", "да", "", "", "заметка 2"]
    store = SheetStore({"Events": [list(EVENT_HEADERS), first, [], second]})

    _sync(store)
    _sync(store)

    rows = store.sheets["Events"]
    assert [r[1] if len(r) > 1 else "" for r in rows[1:]] == ["Первое", "", "Второе"]  # nothing shifted
    assert [r[9] if len(r) > 9 else "" for r in rows[1:]] == ["заметка 1", "", "заметка 2"]
    assert len(_records(EVENTS)) == 2


# --- yearly events entered with a past date ----------------------------------------------


def _notifications_text() -> list[str]:
    from sqlalchemy import select

    from app.models.notification import Notification

    async def _r() -> list[str]:
        async with db.get_session() as session:
            return [n.text for n in (await session.execute(select(Notification))).scalars()]

    return asyncio.run(_r())


def test_yearly_event_with_past_date_moves_on_without_a_stale_reminder(schema: None) -> None:
    from app.jobs import daily_reminders
    from tests.test_google_sheets_two_way import SheetStore
    from app.google_sheets.mappers import EVENT_HEADERS

    store = SheetStore({"Events": [list(EVENT_HEADERS),
                                   ["", "День рождения", "24.08.2026", "каждый год", "за 7 дней", "Купить подарок", ""]]})
    services = Services(settings=Settings(_env_file=None), sheets=store)  # type: ignore[arg-type]
    _sync(store)

    asyncio.run(daily_reminders(services, today=date(2026, 10, 3)))  # the date has passed
    assert _notifications_text() == []  # no "уже прошло" reminder
    assert _records(EVENTS)[0].next_date == date(2027, 8, 24)  # moved to the next occurrence

    asyncio.run(daily_reminders(services, today=date(2027, 8, 17)))  # 7 days before
    [text] = _notifications_text()
    assert "День рождения" in text and "через 7 дн." in text


def test_reminder_missed_while_the_app_was_down_is_still_sent_late(schema: None) -> None:
    from app.jobs import daily_reminders

    services = Services(settings=Settings(_env_file=None))
    _sync(_store(EVENTS, ["", "Встреча", "2026-12-24", "none", "3", "", ""]))  # reminder due 21.12
    asyncio.run(daily_reminders(services, today=date(2026, 12, 26)))  # app was down until after the date
    [text] = _notifications_text()
    assert "Встреча" in text and "уже прошло" in text
