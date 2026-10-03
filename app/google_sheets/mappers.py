"""Mapping between SQLAlchemy/domain objects and Google Sheets rows.

This layer is deliberately separate from both the transport client and the
SQLAlchemy models: it owns the human-facing representation (headers, ISO dates,
display money, "да"/"нет" booleans) and turns raw sheet cells back into strictly
validated values. Domain functions (``update_event``, ``update_expense``) remain
the only path that mutates state.
"""

from __future__ import annotations

import re
import secrets
from datetime import date, datetime

from app.models.event import Event, EventRecurrence
from app.models.expense import ExpensePeriod, RecurringExpense
from app.models.reminder import Reminder

EVENT_HEADERS = ["ID", "Событие", "Дата", "Повтор", "Напоминание", "Что сделать", "Статус"]
EXPENSE_HEADERS = [
    "ID",
    "Название",
    "Сумма",
    "Валюта",
    "Период",
    "День оплаты",
    "Категория",
    "Следующая оплата",
    "Активен",
    "Напомнить за (дн.)",
]
REMINDER_HEADERS = ["ID", "Event ID", "Напомнить", "Выполнено", "Отправлено", "Отправлено в"]
SETTINGS_HEADERS = ["Параметр", "Значение"]

_TRUE = "да"
_FALSE = "нет"


# --- scalar helpers --------------------------------------------------------

def bool_to_str(value: bool) -> str:
    return _TRUE if value else _FALSE


def str_to_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == _TRUE:
        return True
    if normalized == _FALSE:
        return False
    raise ValueError(f"Invalid boolean: {value!r} (expected '{_TRUE}' or '{_FALSE}')")


def date_to_str(value: date) -> str:
    return value.isoformat()


_RU_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def str_to_date(value: str) -> date:
    """Parse ``YYYY-MM-DD`` or ``DD.MM.YYYY``.

    The spreadsheet uses the ru_RU locale: a date typed by hand becomes a date
    cell that Sheets renders (and the API returns as FORMATTED_VALUE) as
    ``DD.MM.YYYY``. The dotted form is unambiguous, so both are accepted.
    """
    text = value.strip()
    match = _RU_DATE_RE.match(text)
    try:
        if match:
            day, month, year = (int(part) for part in match.groups())
            return date(year, month, day)
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"Invalid date: {value!r} (expected YYYY-MM-DD or DD.MM.YYYY)"
        ) from exc


def datetime_to_str(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def minor_to_display(amount_minor: int) -> str:
    """Integer minor units -> human-readable amount string (pure integers)."""
    whole, fraction = divmod(amount_minor, 100)
    return f"{whole}.{fraction:02d}"


def display_to_minor(value: str) -> int:
    """Strictly parse a display amount ("12.50" or "12,50") to minor units.

    Rejects ambiguous inputs (e.g. "1,234.56") and anything with more than two
    decimal places. Stays in integer arithmetic throughout.
    """
    text = value.strip()
    if not text:
        raise ValueError("Amount is empty")
    if "," in text and "." in text:
        raise ValueError(f"Ambiguous amount: {value!r}")
    if "," in text:
        text = text.replace(",", ".")
    if text.count(".") > 1:
        raise ValueError(f"Invalid amount: {value!r}")

    if "." in text:
        whole, fraction = text.split(".")
    else:
        whole, fraction = text, ""

    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        raise ValueError(f"Invalid amount: {value!r}")
    if len(fraction) > 2:
        raise ValueError(f"Amount has more than 2 decimal places: {value!r}")

    fraction = (fraction + "00")[:2]
    return int(whole) * 100 + int(fraction)


def offsets_to_str(offsets: list[int]) -> str:
    return ", ".join(str(v) for v in offsets)


def str_to_offsets(value: str) -> list[int]:
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        raise ValueError(f"Invalid reminder offsets: {value!r}")
    try:
        values = [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"Invalid reminder offsets: {value!r}") from exc
    if any(v < 0 for v in values):
        raise ValueError("Reminder offsets must be non-negative days")
    return sorted(set(values))


def _recurrence(value: str) -> EventRecurrence:
    try:
        return EventRecurrence(value.strip().lower())
    except ValueError as exc:
        raise ValueError(f"Invalid recurrence: {value!r}") from exc


def _period(value: str) -> ExpensePeriod:
    try:
        return ExpensePeriod(value.strip().lower())
    except ValueError as exc:
        raise ValueError(f"Invalid period: {value!r}") from exc


def _parse_int(value: str, label: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {value!r}") from exc


def _cell(row: list[object], index: int) -> object | None:
    return row[index] if index < len(row) else None


def _cell_text(row: list[object], index: int) -> str | None:
    raw = _cell(row, index)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


# --- row identity ------------------------------------------------------------

# A new row is first stamped with a one-time pending key in its ID cell; the
# record is then created with the same ``sheet_key``. If the process stops
# before the real ID is exported, the next sync finds the record by this key
# instead of creating a duplicate.
_PENDING_KEY_PREFIX = "new-"
_PENDING_KEY_RE = re.compile(r"^new-[0-9a-f]{12}$")


def new_pending_key() -> str:
    return _PENDING_KEY_PREFIX + secrets.token_hex(6)


def parse_row_key(row: list[object]) -> int | str | None:
    """Return the row's identity: an int ID, a pending key, or ``None`` (new row).

    Raises ``ValueError`` for anything else in the ID cell.
    """

    raw = _cell_text(row, 0)
    if raw is None:
        return None
    if _PENDING_KEY_RE.match(raw):
        return raw
    return _parse_int(raw, "ID")


def _require(fields: dict, labels: dict[str, str]) -> None:
    missing = [label for name, label in labels.items() if name not in fields]
    if missing:
        raise ValueError("Required for a new row: " + ", ".join(missing))


def require_new_event_fields(fields: dict) -> None:
    """Raise ``ValueError`` unless ``fields`` can create a new event."""

    _require(fields, {"name": EVENT_HEADERS[1], "next_date": EVENT_HEADERS[2]})


def require_new_expense_fields(fields: dict) -> None:
    """Raise ``ValueError`` unless ``fields`` can create a new expense."""

    _require(
        fields,
        {
            "name": EXPENSE_HEADERS[1],
            "amount_minor": EXPENSE_HEADERS[2],
            "currency": EXPENSE_HEADERS[3],
            "payment_day": EXPENSE_HEADERS[5],
            "next_payment_date": EXPENSE_HEADERS[7],
        },
    )


# --- Events ----------------------------------------------------------------

def event_to_row(event: Event) -> list[object]:
    return [
        event.id,
        event.name,
        date_to_str(event.next_date),
        event.recurrence.value,
        offsets_to_str(event.reminder_offsets or [0]),
        event.action_text or "",
        bool_to_str(event.is_active),
    ]


def parse_event_row(row: list[object]) -> dict:
    """Parse a sheet row into ``{"id": <row key>, **fields}``.

    ``id`` is an int ID, a pending key or ``None`` for a new row (see
    :func:`parse_row_key`). Empty cells are treated as "leave unchanged" and
    omitted from the result. Raises ``ValueError`` for any invalid value.
    """
    fields: dict = {"id": parse_row_key(row)}

    name = _cell_text(row, 1)
    if name is not None:
        fields["name"] = name

    next_date = _cell_text(row, 2)
    if next_date is not None:
        fields["next_date"] = str_to_date(next_date)

    recurrence = _cell_text(row, 3)
    if recurrence is not None:
        fields["recurrence"] = _recurrence(recurrence)

    offsets = _cell_text(row, 4)
    if offsets is not None:
        fields["reminder_offsets"] = str_to_offsets(offsets)

    action_text = _cell_text(row, 5)
    if action_text is not None:
        fields["action_text"] = action_text

    active = _cell_text(row, 6)
    if active is not None:
        fields["is_active"] = str_to_bool(active)

    return fields


# --- Expenses --------------------------------------------------------------

def expense_to_row(expense: RecurringExpense) -> list[object]:
    return [
        expense.id,
        expense.name,
        minor_to_display(expense.amount_minor),
        expense.currency,
        expense.period.value,
        expense.payment_day,
        expense.category or "",
        date_to_str(expense.next_payment_date) if expense.next_payment_date else "",
        bool_to_str(expense.is_active),
        expense.reminder_days_before,
    ]


def parse_expense_row(row: list[object]) -> dict:
    """Parse a sheet row into ``{"id": <row key>, **fields}`` (see parse_event_row)."""
    fields: dict = {"id": parse_row_key(row)}

    name = _cell_text(row, 1)
    if name is not None:
        fields["name"] = name

    amount = _cell_text(row, 2)
    if amount is not None:
        fields["amount_minor"] = display_to_minor(amount)

    currency = _cell_text(row, 3)
    if currency is not None:
        fields["currency"] = currency

    period = _cell_text(row, 4)
    if period is not None:
        fields["period"] = _period(period)

    payment_day = _cell_text(row, 5)
    if payment_day is not None:
        day = _parse_int(payment_day, "payment day")
        if not 1 <= day <= 31:
            raise ValueError(f"Payment day must be between 1 and 31: {payment_day!r}")
        fields["payment_day"] = day

    category = _cell_text(row, 6)
    if category is not None:
        fields["category"] = category

    next_payment_date = _cell_text(row, 7)
    if next_payment_date is not None:
        fields["next_payment_date"] = str_to_date(next_payment_date)

    active = _cell_text(row, 8)
    if active is not None:
        fields["is_active"] = str_to_bool(active)

    reminder_days = _cell_text(row, 9)
    if reminder_days is not None:
        days = _parse_int(reminder_days, "reminder days")
        if days < 0:
            raise ValueError(f"Reminder days must be 0 or more: {reminder_days!r}")
        fields["reminder_days_before"] = days

    return fields


# --- Reminders (export-only) ----------------------------------------------

def reminder_to_row(reminder: Reminder) -> list[object]:
    return [
        reminder.id,
        reminder.event_id,
        date_to_str(reminder.remind_at),
        bool_to_str(reminder.is_done),
        bool_to_str(reminder.is_sent),
        datetime_to_str(reminder.sent_at) if reminder.sent_at else "",
    ]
