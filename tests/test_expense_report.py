"""Expense report in Telegram: understanding the question, the period boundaries,
the schedule-based calculation, the reply text and its use by the bot.

"Today" is fixed to 2026-10-10 in every test. The report reads the database
(which mirrors the sheet), so Google Sheets is only involved through the sync
that fills it; a row the sheet sync rejected is never counted.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from sqlalchemy.exc import OperationalError

from app import db, expenses
from app.expense_report import (
    Clarify,
    Period,
    build_report,
    format_money,
    format_report,
    last_months_period,
    month_period,
    parse_request,
    reply,
)
from app.models.expense import ExpensePeriod, RecurringExpense
from app.telegram import bot
from tests.test_google_sheets_two_way import SheetStore, _sync
from tests.test_sheet_ux import _sync_ignoring_row_errors

TODAY = date(2026, 10, 10)


def _run(coro):
    return asyncio.run(coro)


def _period(text: str) -> Period:
    result = parse_request(text, TODAY)
    assert isinstance(result, Period), result
    return result


# --- understanding the question ----------------------------------------------------------------


def test_this_month_runs_from_the_first_day_to_today() -> None:
    period = _period("Сколько я потратил за этот месяц?")
    assert (period.start, period.end, period.label) == (date(2026, 10, 1), TODAY, "октябрь 2026")
    assert period.months == ((2026, 10),) and not period.breakdown


@pytest.mark.parametrize(
    "text, first_month, months, label",
    [
        ("Сколько потратил за последние 3 месяца?", date(2026, 8, 1), 3, "август — октябрь 2026"),
        ("Покажи расходы за квартал", date(2026, 8, 1), 3, "август — октябрь 2026"),
        ("сколько потратил за три месяца", date(2026, 8, 1), 3, "август — октябрь 2026"),
        ("Сколько потратил за последние полгода?", date(2026, 5, 1), 6, "май — октябрь 2026"),
        ("расходы за 6 месяцев", date(2026, 5, 1), 6, "май — октябрь 2026"),
        ("Сколько потратил за год?", date(2025, 11, 1), 12, "ноябрь 2025 — октябрь 2026"),
        ("расходы за последние 12 месяцев", date(2025, 11, 1), 12, "ноябрь 2025 — октябрь 2026"),
    ],
)
def test_last_months_include_the_current_one_and_end_today(text, first_month, months, label) -> None:
    period = _period(text)
    assert (period.start, period.end, len(period.months), period.label) == (first_month, TODAY, months, label)
    assert period.months[-1] == (2026, 10) and period.breakdown


def test_a_specific_month_covers_it_whole() -> None:
    period = _period("Покажи расходы за март 2026")
    assert (period.start, period.end, period.label) == (date(2026, 3, 1), date(2026, 3, 31), "март 2026")


def test_a_month_without_a_year_means_this_year() -> None:
    assert _period("Сколько потратил в январе этого года?").start == date(2026, 1, 1)
    assert _period("расходы за май").label == "май 2026"
    assert _period("расходы за март прошлого года").label == "март 2025"


def test_a_month_that_has_not_come_yet_needs_a_year() -> None:
    result = parse_request("сколько потратил в декабре", TODAY)
    assert isinstance(result, Clarify) and "Уточните год" in result.message
    assert _period("сколько потратил в декабре 2026").label == "декабрь 2026"  # an explicit year is fine


def test_the_current_month_named_explicitly_is_the_whole_month_to_today() -> None:
    period = _period("расходы за октябрь")
    assert (period.start, period.end) == (date(2026, 10, 1), TODAY)


def test_period_boundaries_across_a_year_and_a_leap_february() -> None:
    january = last_months_period(3, date(2026, 1, 15))
    assert (january.start, january.months) == (date(2025, 11, 1), ((2025, 11), (2025, 12), (2026, 1)))
    assert month_period(2024, 2, TODAY).end == date(2024, 2, 29)
    assert month_period(2026, 2, TODAY).end == date(2026, 2, 28)


@pytest.mark.parametrize(
    "text",
    ["Сравни расходы за январь и февраль 2026", "расходы за январь и февраль"],
)
def test_comparing_periods_is_not_supported_yet(text: str) -> None:
    assert isinstance(parse_request(text, TODAY), Clarify)


@pytest.mark.parametrize("text", ["Сколько я потратил?", "покажи расходы"])
def test_a_question_without_a_period_is_clarified(text: str) -> None:
    result = parse_request(text, TODAY)
    assert isinstance(result, Clarify) and "За какой период" in result.message


@pytest.mark.parametrize("text", ["потратил за 5 месяцев", "сколько потратил за пять месяцев", "расходы за 2 месяца"])
def test_unsupported_lengths_are_explained_not_guessed(text: str) -> None:
    result = parse_request(text, TODAY)
    assert isinstance(result, Clarify) and "не поддерживается" in result.message


@pytest.mark.parametrize("text", ["привет", "", "   ", None, "какая погода", "Сколько я трачу на подписки?"])
def test_other_text_is_not_an_expense_question(text) -> None:
    assert parse_request(text, TODAY) is None


# --- calculation ------------------------------------------------------------------------------


def _expense(**kw) -> RecurringExpense:
    values = dict(
        name="x", amount_minor=80000, currency="RUB", period=ExpensePeriod.MONTHLY, payment_day=30,
        next_payment_date=date(2026, 10, 30), is_active=True,
    )
    values.update(kw)
    return RecurringExpense(**values)


def test_monthly_payments_are_counted_until_today_only() -> None:
    report = build_report(_period("расходы за 3 месяца"), [_expense()])  # 30 Aug, 30 Sep; 30 Oct is still ahead
    assert report.payments == 2 and report.totals == {"RUB": 160000}
    assert report.by_month == {(2026, 8): {"RUB": 80000}, (2026, 9): {"RUB": 80000}, (2026, 10): {}}


def test_quarterly_and_yearly_payments_follow_their_schedule() -> None:
    quarterly = _expense(
        currency="USD", amount_minor=1050, period=ExpensePeriod.QUARTERLY, payment_day=15,
        next_payment_date=date(2026, 10, 15),
    )
    yearly = _expense(period=ExpensePeriod.YEARLY, payment_day=20, next_payment_date=date(2026, 3, 20), amount_minor=500000)
    half_year = build_report(_period("расходы за полгода"), [quarterly, yearly])
    assert half_year.totals == {"USD": 1050} and half_year.payments == 1  # 15 Jul; the yearly 20 Mar is outside May-Oct
    year = build_report(_period("расходы за год"), [quarterly, yearly])
    assert year.totals == {"USD": 3150, "RUB": 500000} and year.payments == 4  # 3 quarterly + 1 yearly


def test_a_day_31_payment_is_clamped_to_the_end_of_a_short_month() -> None:
    expense = _expense(payment_day=31, next_payment_date=date(2026, 10, 31))
    report = build_report(month_period(2024, 2, TODAY), [expense])
    assert report.payments == 1  # 29 February 2024


def test_an_expense_without_a_next_payment_date_is_skipped_and_reported() -> None:
    report = build_report(_period("расходы за 3 месяца"), [_expense(), _expense(next_payment_date=None)])
    assert (report.payments, report.skipped, report.active) == (2, 1, 2)
    assert "Пропущено платежей без даты следующей оплаты: 1" in format_report(report)


def test_nothing_in_the_period_is_stated_plainly() -> None:
    text = format_report(build_report(_period("расходы за этот месяц"), [_expense()]))
    assert text.startswith("Расходы за октябрь 2026: платежей по графику нет.")


def test_no_active_expenses_at_all_is_stated_plainly() -> None:
    assert "нет активных платежей" in format_report(build_report(_period("расходы за этот месяц"), []))


@pytest.mark.parametrize(
    "minor, currency, text",
    [(2435000, "RUB", "24 350 ₽"), (1050, "USD", "10,50 $"), (5, "EUR", "0,05 €"), (100000000, "RUB", "1 000 000 ₽"), (100, "KZT", "1 KZT")],
)
def test_money_is_formatted_from_integers(minor, currency, text) -> None:
    assert format_money(minor, currency) == text


def test_the_report_text_has_total_count_breakdown_and_the_basis() -> None:
    text = format_report(build_report(_period("расходы за 3 месяца"), [_expense()]))
    assert text.splitlines() == [
        "Расходы за август — октябрь 2026: 1 600 ₽",
        "Платежей по графику: 2",
        "Разбивка:",
        "• Август — 800 ₽",
        "• Сентябрь — 800 ₽",
        "• Октябрь — платежей нет",
        "Расчёт по графику повторяющихся платежей (фактические оплаты не отслеживаются).",
    ]


def test_currencies_are_never_added_together() -> None:
    usd = _expense(currency="USD", amount_minor=1000)
    text = format_report(build_report(_period("расходы за 3 месяца"), [_expense(), usd]))
    assert "1 600 ₽ + 20 $" in text and "Валюты не пересчитываются" in text


def test_a_single_month_has_no_breakdown() -> None:
    text = format_report(build_report(month_period(2026, 9, TODAY), [_expense()]))
    assert "Разбивка" not in text and "Расходы за сентябрь 2026: 800 ₽" in text


# --- the database and the bot --------------------------------------------------------------------


async def _add(**kw) -> None:
    values = dict(name="t2 sim", amount_minor=80000, currency="RUB", period="monthly", payment_day=30,
                  next_payment_date=date(2026, 10, 30))
    values.update(kw)
    async with db.get_session() as session:
        await expenses.create_expense(session, **values)
        await session.commit()


def test_the_reply_reads_active_expenses_from_the_database(schema: None) -> None:
    async def scenario() -> str:
        await _add()
        await _add(name="paused", amount_minor=99900)
        async with db.get_session() as session:
            [_, paused] = await expenses.list_expenses(session, active_only=False)
            paused.is_active = False
            await session.commit()
        return await reply("Сколько я потратил за последние 3 месяца?", today=TODAY)

    text = _run(scenario())
    assert "Расходы за август — октябрь 2026: 1 600 ₽" in text and "Платежей по графику: 2" in text  # the paused one is not counted


def test_an_empty_database_says_there_is_nothing(schema: None) -> None:
    assert "нет активных платежей" in _run(reply("расходы за полгода", today=TODAY))


def test_a_database_error_is_reported_not_shown_as_zero(schema: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken():
        raise OperationalError("select", {}, Exception("disk I/O error"))

    monkeypatch.setattr(db, "get_session", broken)
    text = _run(reply("Сколько я потратил за этот месяц?", today=TODAY))
    assert "Не удалось прочитать" in text and "0" not in text


def test_rows_rejected_by_the_sheet_are_not_counted(schema: None) -> None:
    store = SheetStore({"Expenses": [
        ["ID", "Название", "Сумма", "Валюта", "Период", "День оплаты", "Категория", "Следующая оплата", "Активен", "Напомнить за"],
        ["", "Хороший", "800", "RUB", "monthly", "30", "", "2026-10-30", "", ""],
        ["", "Плохой", "1,234.56", "RUB", "monthly", "30", "", "2026-10-30", "", ""],  # an ambiguous amount: rejected
    ]})
    _sync_ignoring_row_errors(store)
    text = _run(reply("расходы за 3 месяца", today=TODAY))
    assert "1 600 ₽" in text and "Платежей по графику: 2" in text


def test_the_bot_answers_an_expense_question(schema: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(_add())
    monkeypatch.setattr("app.expense_report._today", lambda: TODAY)
    text = _run(bot.answer("Сколько я потратил за этот месяц?", []))
    assert text.startswith("Расходы за октябрь 2026:")


def test_the_bot_keeps_its_commands_and_its_explanation(schema: None) -> None:
    assert _run(bot.answer("/help", [])) == bot.HELP_TEXT
    assert _run(bot.answer("привет", [])) == bot.UNSUPPORTED_TEXT
    assert "расходы" in bot.HELP_TEXT.lower()


def test_a_message_from_the_owner_gets_the_report_reply(schema: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(_add())
    monkeypatch.setattr("app.expense_report._today", lambda: TODAY)
    sent: list[tuple[int, str]] = []

    class Telegram:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    update = {"update_id": 1, "message": {"chat": {"id": 7}, "text": "Покажи расходы за март 2026"}}
    _run(bot.handle_update(Telegram(), update, chat_id=7, job_names=[]))
    assert sent and sent[0][0] == 7 and sent[0][1].startswith("Расходы за март 2026")


def test_present_tense_and_category_questions() -> None:
    assert _period("Сколько я трачу за этот месяц?").label == "октябрь 2026"  # "трачу" is a spending question too
    for text in ("Сколько я трачу на подписки?", "расходы на еду", "сколько потратил на такси за март 2026"):
        assert parse_request(text, TODAY) is None  # by category: not supported, the usual explanation is shown
