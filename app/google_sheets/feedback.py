"""Row feedback in the sheet: colour and a note on the ID cell.

After every sync each data row gets, on its ID cell (column A):

* green + a note "how the bot understood this row" — the values as stored in
  SQLite (including the computed reminder dates), not an echo of the input;
* red + a note with the reason the row was not accepted.

The note is shown on hover. A deleted row has its note and colour removed.
Times are shown in Moscow time (fixed UTC+3, no daylight saving).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.dates import next_occurrence, next_reminder_date
from app.google_sheets.client import GoogleSheetsClient
from app.google_sheets.mappers import date_to_ru, minor_to_display
from app.google_sheets.setup import ROLE_EMAIL, ROLE_EVENTS, ROLE_EXPENSES, SheetLayout
from app.google_sheets.sync import RowReport
from app.models.allowed_sender import AllowedSender
from app.models.event import Event, EventRecurrence
from app.models.expense import RecurringExpense

logger = logging.getLogger(__name__)

MOSCOW = timezone(timedelta(hours=3), "МСК")
GREEN = {"red": 0.576, "green": 0.769, "blue": 0.490}  # #93c47d
RED = {"red": 0.878, "green": 0.400, "blue": 0.400}  # #e06666

_PERIODS = {"monthly": "ежемесячно", "quarterly": "ежеквартально", "yearly": "ежегодно"}


def stamp(now: datetime | None = None) -> str:
    """``03.10.2026 19:42 МСК`` for ``now`` (UTC if naive/aware)."""

    moment = (now or datetime.now(timezone.utc)).astimezone(MOSCOW)
    return moment.strftime("%d.%m.%Y %H:%M") + " МСК"


def _days_before(days: int) -> str:
    return "в день события" if days == 0 else f"за {days} дн."


def event_note(event: Event, today: date, now: datetime | None = None) -> str:
    lines = [f"✓ Принято · {stamp(now)}", f"Событие: {event.name}"]
    yearly = event.recurrence is EventRecurrence.YEARLY
    lines.append("Повтор: каждый год" if yearly else "Повтор: один раз")
    if not event.is_active:
        lines.append("Активно: нет — на паузе, напоминаний не будет")
    else:
        happens = next_occurrence(event.recurrence, event.next_date, event.anchor_date, today)
        if happens is None:
            lines.append(f"Когда случится: {date_to_ru(event.next_date)} (уже прошло)")
        else:
            lines.append(f"Когда случится: {date_to_ru(happens)}")
            reminders = [
                f"{_days_before(days)} → {date_to_ru(happens - timedelta(days=days))}"
                for days in sorted(set(event.reminder_offsets or [0]), reverse=True)
            ]
            lines.append("Напоминания: " + "; ".join(reminders))
            following = next_reminder_date(
                event.recurrence, event.next_date, event.anchor_date, event.reminder_offsets or [0], today
            )
            lines.append(f"Ближайшее напоминание: {date_to_ru(following)}")
        lines.append("Активно: да")
    if event.action_text:
        lines.append(f"Что сделать: {event.action_text}")
    return "\n".join(lines)


def expense_note(expense: RecurringExpense, today: date, now: datetime | None = None) -> str:
    lines = [
        f"✓ Принято · {stamp(now)}",
        f"Платёж: {expense.name} — {minor_to_display(expense.amount_minor)} {expense.currency}",
        f"Период: {_PERIODS.get(expense.period.value, expense.period.value)}"
        f" · день оплаты: {expense.payment_day}",
    ]
    if expense.next_payment_date:
        remind = expense.next_payment_date - timedelta(days=expense.reminder_days_before)
        lines.append(f"Следующая оплата: {date_to_ru(expense.next_payment_date)}")
        lines.append(f"Напоминание: за {expense.reminder_days_before} дн. → {date_to_ru(remind)}")
    if expense.category:
        lines.append(f"Категория: {expense.category}")
    lines.append("Активен: да" if expense.is_active else "Активен: нет — на паузе, напоминаний не будет")
    return "\n".join(lines)


def sender_note(sender: AllowedSender, today: date, now: datetime | None = None) -> str:
    lines = [f"✓ Принято · {stamp(now)}", f"Отправитель: {sender.email}"]
    if sender.is_active:
        lines.append("Активно: да — письма от этого адреса читаются и попадают в Inbox")
    else:
        lines.append("Активно: нет — на паузе, письма от этого адреса не читаются")
    lines.append("Адрес сравнивается точно (регистр не важен), весь домен не разрешается")
    return "\n".join(lines)


def error_note(message: str, now: datetime | None = None) -> str:
    return (
        f"✗ Строка не принята · {stamp(now)}\n{message}\n"
        "Исправьте ячейку — строка проверится заново."
    )


def _cell_update(sheet_id: int, row_number: int, note: str | None, colour: dict | None) -> dict:
    """``updateCells`` for A{row}: set note+colour, or clear both when ``None``."""

    cell: dict = {}
    if note is not None:
        cell["note"] = note
    if colour is not None:
        cell["userEnteredFormat"] = {"backgroundColor": colour}
    return {
        "updateCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_number - 1,
                "endRowIndex": row_number,
                "startColumnIndex": 0,
                "endColumnIndex": 1,
            },
            "rows": [{"values": [cell]}],
            "fields": "note,userEnteredFormat.backgroundColor",
        }
    }


async def build_requests(
    session: AsyncSession,
    layout: SheetLayout,
    reports: dict[str, list[RowReport]],
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> list[dict]:
    today = today or date.today()
    requests: list[dict] = []
    models = {
        ROLE_EVENTS: (Event, event_note),
        ROLE_EXPENSES: (RecurringExpense, expense_note),
        ROLE_EMAIL: (AllowedSender, sender_note),
    }
    for role, (model, note_for) in models.items():
        sheet_id = layout[role].sheet_id
        for report in reports.get(role, []):
            if report.deleted:
                requests.append(_cell_update(sheet_id, report.row_number, None, None))
            elif report.error is not None:
                requests.append(
                    _cell_update(sheet_id, report.row_number, error_note(report.error, now), RED)
                )
            else:
                record = await session.get(model, report.record_id)
                if record is not None:
                    note = note_for(record, today, now)
                    requests.append(_cell_update(sheet_id, report.row_number, note, GREEN))
    return requests


async def apply_feedback(
    session: AsyncSession,
    client: GoogleSheetsClient,
    layout: SheetLayout,
    reports: dict[str, list[RowReport]],
) -> None:
    """Write colours and notes for every reported row in ONE request."""

    await client.batch_update(await build_requests(session, layout, reports))
