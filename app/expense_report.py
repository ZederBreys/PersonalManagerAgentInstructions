"""Expense report for Telegram: "сколько я потратил за ..." (read-only).

The question is understood by plain rules (no LLM needed for this small set of
phrases) and everything else is deterministic Python:

1. :func:`parse_request` turns the text into an explicit period (or a short
   clarifying question, or ``None`` when this is not an expense question);
2. :func:`build_report` counts, for every active recurring expense, the payments
   its schedule puts inside the period (the same ``count_payments`` rule the
   reminders use) and sums them in integer minor units, per currency;
3. :func:`format_report` renders the answer.

What is counted: the sheet "Расходники" lists *recurring payments*, not a ledger
of actual purchases, so the report is the cash outflow **by schedule**. Past
periods are computed from the payments' current settings. The sheet and the
database hold the same, already validated rows (the sheet is mirrored into the
database every few seconds); the report reads the database.
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy.exc import SQLAlchemyError

from app import db, expenses
from app.expense_dates import count_payments
from app.models.expense import RecurringExpense

logger = logging.getLogger(__name__)

_MONTH_NAMES = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]
_MONTH_RE = re.compile(
    r"\b(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]\b|июн\w*|июл\w*|"
    r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)"
)
_MONTH_STEMS = ["январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]
_YEAR_RE = re.compile(r"\b(20\d\d)\b")
_EXPENSE_RE = re.compile(r"потрат|трат|трач|расход|платеж")
_COMPARE_RE = re.compile(r"сравн|сопостав")
# "на подписки", "на еду": a category question, which is not supported
_TOPIC_RE = re.compile(
    r"\bна\s+(?!этот\b|текущ|прошл|последн|весь|все\b|\d|три\b|шесть\b|двенадцать\b"
    r"|год|месяц|квартал|полгод|январ|феврал|март|апрел|ма[йяе]\b|июн|июл|август|сентябр|октябр|ноябр|декабр)\w+"
)
_NUMBER_WORDS = {
    "один": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
}
_MONTHS_COUNT_RE = re.compile(r"\b(\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")\s+месяц\w*")
_SUPPORTED_N = (3, 6, 12)

MIN_YEAR, MAX_YEAR = 2000, 2100

PERIOD_HINT = "за этот месяц, за 3 месяца (или квартал), за полгода, за год или за конкретный месяц, например «за март 2026»"
CURRENCY_SYMBOLS = {"RUB": "₽", "USD": "$", "EUR": "€"}


@dataclass(frozen=True)
class Period:
    """An explicit, inclusive period split into calendar months."""

    start: date
    end: date
    label: str
    months: tuple[tuple[int, int], ...]  # (year, month), oldest first

    @property
    def breakdown(self) -> bool:
        return len(self.months) >= 3


@dataclass(frozen=True)
class Clarify:
    """The question cannot be answered as asked: say what is needed."""

    message: str


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").replace("?", " ").replace(",", " ").split())


def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _shift(year: int, month: int, delta: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def _month_label(year: int, month: int) -> str:
    return f"{_MONTH_NAMES[month - 1]} {year}"


def _range_label(first: tuple[int, int], last: tuple[int, int]) -> str:
    if first[0] == last[0]:
        return f"{_MONTH_NAMES[first[1] - 1]} — {_MONTH_NAMES[last[1] - 1]} {last[0]}"
    return f"{_month_label(*first)} — {_month_label(*last)}"


def month_period(year: int, month: int, today: date) -> Period:
    """One calendar month; the current month ends today, other months at their last day."""

    end = _month_end(year, month)
    if (year, month) == (today.year, today.month):
        end = today
    return Period(_month_start(year, month), end, _month_label(year, month), ((year, month),))


def last_months_period(count: int, today: date) -> Period:
    """The last ``count`` calendar months including the current one, up to today."""

    first = _shift(today.year, today.month, -(count - 1))
    months = tuple(_shift(first[0], first[1], i) for i in range(count))
    return Period(_month_start(*first), today, _range_label(first, months[-1]), months)


def _month_number(word: str) -> int:
    for number, stem in enumerate(_MONTH_STEMS, start=1):
        if word.startswith(stem):
            return number
    raise ValueError(word)


def parse_request(text: str | None, today: date) -> Period | Clarify | None:
    """Understand an expense question; ``None`` means "not an expense question"."""

    normalized = _normalize(text or "")
    if not normalized or not _EXPENSE_RE.search(normalized):
        return None
    if _COMPARE_RE.search(normalized):
        return Clarify("Сравнение периодов пока не поддерживается. Спросите про один период: " + PERIOD_HINT + ".")

    if _TOPIC_RE.search(normalized):
        return None  # spending on a category/topic: not supported, the usual explanation is shown

    months = sorted({_month_number(word) for word in _MONTH_RE.findall(normalized)})
    if len(months) > 1:
        return Clarify("Назовите один месяц, например «за март 2026». Несколько месяцев сразу пока не поддерживаются.")
    if months:
        return _specific_month(months[0], normalized, today)

    counted = _MONTHS_COUNT_RE.search(normalized)
    if counted:
        word = counted.group(1)
        number = int(word) if word.isdigit() else _NUMBER_WORDS.get(word)
        if number in _SUPPORTED_N:
            return last_months_period(number, today)
        if number == 1:
            return month_period(today.year, today.month, today)
        return Clarify(f"Период «{counted.group(0)}» не поддерживается. Доступно: {PERIOD_HINT}.")
    if "полгода" in normalized or "пол года" in normalized:
        return last_months_period(6, today)
    if "квартал" in normalized:
        return last_months_period(3, today)
    if re.search(r"\bгод\w*\b", normalized):
        return last_months_period(12, today)
    if re.search(r"\bмесяц\w*\b", normalized):
        return month_period(today.year, today.month, today)
    return Clarify("За какой период? Например: " + PERIOD_HINT + ".")


def _specific_month(month: int, normalized: str, today: date) -> Period | Clarify:
    explicit = _YEAR_RE.search(normalized)
    if explicit:
        year = int(explicit.group(1))
    elif "прошл" in normalized and re.search(r"\bгод", normalized):
        year = today.year - 1
    else:
        year = today.year
        if (year, month) > (today.year, today.month):
            return Clarify(
                f"{_month_label(year, month).capitalize()} ещё не наступил. Уточните год, "
                f"например «за {_MONTH_NAMES[month - 1]} {year - 1}»."
            )
    if not MIN_YEAR <= year <= MAX_YEAR:
        return Clarify(f"Год {year} вне поддерживаемого диапазона ({MIN_YEAR}–{MAX_YEAR}).")
    return month_period(year, month, today)


# --- calculation ----------------------------------------------------------------------


@dataclass
class Report:
    period: Period
    totals: dict[str, int]  # currency -> minor units, whole period
    by_month: dict[tuple[int, int], dict[str, int]]
    payments: int  # scheduled payments inside the period
    skipped: int  # active expenses with no next payment date (cannot be scheduled)
    active: int  # active expenses considered


def build_report(period: Period, items: list[RecurringExpense]) -> Report:
    """Sum the scheduled payments of ``items`` inside ``period`` (integer arithmetic only)."""

    report = Report(period, {}, {month: {} for month in period.months}, 0, 0, len(items))
    for item in items:
        if item.next_payment_date is None:
            report.skipped += 1
            continue
        for year, month in period.months:
            start = max(period.start, _month_start(year, month))
            end = min(period.end, _month_end(year, month))
            number = count_payments(item.next_payment_date, item.period, item.payment_day, start, end)
            if not number:
                continue
            amount = number * item.amount_minor
            by_currency = report.by_month[(year, month)]
            by_currency[item.currency] = by_currency.get(item.currency, 0) + amount
            report.totals[item.currency] = report.totals.get(item.currency, 0) + amount
            report.payments += number
    return report


# --- text -------------------------------------------------------------------------------


def format_money(amount_minor: int, currency: str) -> str:
    whole, fraction = divmod(amount_minor, 100)
    text = f"{whole:,}".replace(",", " ")
    if fraction:
        text += f",{fraction:02d}"
    return f"{text} {CURRENCY_SYMBOLS.get(currency, currency)}"


def _money_by_currency(totals: dict[str, int]) -> str:
    return " + ".join(format_money(totals[code], code) for code in sorted(totals))


def format_report(report: Report) -> str:
    period = report.period
    if report.active == 0:
        return f"Расходы за {period.label}: нет активных платежей в таблице «Расходники»."
    if report.payments == 0:
        lines = [f"Расходы за {period.label}: платежей по графику нет."]
    else:
        lines = [
            f"Расходы за {period.label}: {_money_by_currency(report.totals)}",
            f"Платежей по графику: {report.payments}",
        ]
        if period.breakdown:
            lines.append("Разбивка:")
            for year, month in period.months:
                totals = report.by_month[(year, month)]
                lines.append(f"• {_MONTH_NAMES[month - 1].capitalize()} — {_money_by_currency(totals) if totals else 'платежей нет'}")
        if len(report.totals) > 1:
            lines.append("Валюты не пересчитываются, суммы указаны отдельно.")
    if report.skipped:
        lines.append(f"Пропущено платежей без даты следующей оплаты: {report.skipped}.")
    lines.append("Расчёт по графику повторяющихся платежей (фактические оплаты не отслеживаются).")
    return "\n".join(lines)


def _today() -> date:
    return date.today()


async def reply(text: str | None, *, today: date | None = None) -> str | None:
    """The answer to an expense question, or ``None`` when ``text`` is not one."""

    request = parse_request(text, today or _today())
    if request is None:
        return None
    if isinstance(request, Clarify):
        return request.message
    try:
        async with db.get_session() as session:
            items = await expenses.list_expenses(session, active_only=True)
    except SQLAlchemyError:
        logger.exception("Expense report: reading the expenses failed")
        return "Не удалось прочитать расходы из базы, отчёт не построен. Попробуйте позже."
    return format_report(build_report(request, items))
