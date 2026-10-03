"""Pure tests for the Google Sheets row mappers."""

from __future__ import annotations

from datetime import date

import pytest

from app.google_sheets.mappers import (
    bool_to_str,
    date_to_str,
    display_to_minor,
    event_to_row,
    expense_to_row,
    minor_to_display,
    offsets_to_str,
    parse_event_row,
    parse_expense_row,
    str_to_bool,
    str_to_date,
    str_to_offsets,
)
from app.models.event import Event, EventRecurrence
from app.models.expense import ExpensePeriod, RecurringExpense


# --- money ------------------------------------------------------------------

def test_minor_to_display():
    assert minor_to_display(1250) == "12.50"
    assert minor_to_display(99) == "0.99"
    assert minor_to_display(1) == "0.01"
    assert minor_to_display(100) == "1.00"
    assert minor_to_display(0) == "0.00"


def test_display_to_minor_accepts_dot_and_comma():
    assert display_to_minor("12.50") == 1250
    assert display_to_minor("12,50") == 1250
    assert display_to_minor("12.5") == 1250
    assert display_to_minor("1.00") == 100


def test_display_to_minor_rejects_invalid():
    for bad in ["1,234.56", "1.234", "abc", "", "1.2.3", "12.345"]:
        with pytest.raises(ValueError):
            display_to_minor(bad)


# --- booleans ---------------------------------------------------------------

def test_bool_roundtrip():
    assert str_to_bool(bool_to_str(True)) is True
    assert str_to_bool(bool_to_str(False)) is False


def test_str_to_bool_rejects_invalid():
    with pytest.raises(ValueError):
        str_to_bool("maybe")


# --- dates ------------------------------------------------------------------

def test_date_roundtrip():
    assert str_to_date(date_to_str(date(2026, 10, 15))) == date(2026, 10, 15)


def test_str_to_date_rejects_invalid():
    with pytest.raises(ValueError):
        str_to_date("2026-13-01")


def test_str_to_date_accepts_ru_locale_format():
    # The production sheet is ru_RU: hand-typed dates come back as DD.MM.YYYY.
    assert str_to_date("24.08.2025") == date(2025, 8, 24)
    assert str_to_date(" 01.01.2026 ") == date(2026, 1, 1)


@pytest.mark.parametrize("value", ["31.02.2025", "1.8.2025", "24.08.25", "24/08/2025", "08.24.2025"])
def test_str_to_date_rejects_invalid_or_ambiguous_ru_dates(value):
    with pytest.raises(ValueError):
        str_to_date(value)


# --- offsets ----------------------------------------------------------------

def test_offsets_roundtrip():
    assert str_to_offsets(offsets_to_str([0, 15])) == [0, 15]


def test_str_to_offsets_dedupes_and_sorts():
    assert str_to_offsets("15,0,15") == [0, 15]


def test_str_to_offsets_rejects_invalid():
    with pytest.raises(ValueError):
        str_to_offsets("a,b")
    with pytest.raises(ValueError):
        str_to_offsets("-1")


# --- event rows -------------------------------------------------------------

def _event(**kwargs):
    defaults = dict(
        id=1,
        name="День рождения",
        next_date=date(2026, 10, 15),
        recurrence=EventRecurrence.YEARLY,
        reminder_offsets=[0, 15],
        action_text="Позвонить",
        is_active=True,
    )
    defaults.update(kwargs)
    return Event(**defaults)


def test_event_to_row():
    row = event_to_row(_event())
    assert row == [1, "День рождения", "2026-10-15", "yearly", "0, 15", "Позвонить", "да"]


def test_parse_event_row_full():
    fields = parse_event_row([1, "ДР", "2026-10-15", "yearly", "0, 15", "сделать", "да"])
    assert fields == {
        "id": 1,
        "name": "ДР",
        "next_date": date(2026, 10, 15),
        "recurrence": EventRecurrence.YEARLY,
        "reminder_offsets": [0, 15],
        "action_text": "сделать",
        "is_active": True,
    }


def test_parse_event_row_empty_cells_omitted():
    fields = parse_event_row([1, "", "", "", "", "", ""])
    assert fields == {"id": 1}


def test_parse_event_row_empty_id_means_new_row():
    assert parse_event_row(["", "x"]) == {"id": None, "name": "x"}


def test_parse_event_row_pending_key_kept_as_text():
    assert parse_event_row(["new-0123456789ab", "x"])["id"] == "new-0123456789ab"


def test_parse_event_row_garbage_id_rejected():
    with pytest.raises(ValueError):
        parse_event_row(["abc", "x"])


def test_parse_event_row_invalid_date_rejected():
    with pytest.raises(ValueError):
        parse_event_row([1, "x", "2026-99-99"])


def test_parse_event_row_invalid_recurrence_rejected():
    with pytest.raises(ValueError):
        parse_event_row([1, "x", "2026-10-15", "weekly"])


# --- expense rows -----------------------------------------------------------

def _expense(**kwargs):
    defaults = dict(
        id=1,
        name="Подписка",
        amount_minor=1250,
        currency="USD",
        period=ExpensePeriod.MONTHLY,
        payment_day=15,
        category="Софт",
        next_payment_date=date(2026, 10, 15),
        is_active=True,
        reminder_days_before=3,
    )
    defaults.update(kwargs)
    return RecurringExpense(**defaults)


def test_expense_to_row():
    row = expense_to_row(_expense())
    assert row == [1, "Подписка", "12.50", "USD", "monthly", 15, "Софт", "2026-10-15", "да", 3]


def test_parse_expense_row_full():
    fields = parse_expense_row(
        [1, "Подписка", "12,50", "usd", "monthly", "15", "Софт", "2026-10-15", "да", "5"]
    )
    assert fields == {
        "id": 1,
        "name": "Подписка",
        "amount_minor": 1250,
        "currency": "usd",
        "period": ExpensePeriod.MONTHLY,
        "payment_day": 15,
        "category": "Софт",
        "next_payment_date": date(2026, 10, 15),
        "is_active": True,
        "reminder_days_before": 5,
    }


def test_parse_expense_row_invalid_money_rejected():
    with pytest.raises(ValueError):
        parse_expense_row([1, "x", "1,234.56"])


def test_parse_expense_row_invalid_period_rejected():
    with pytest.raises(ValueError):
        parse_expense_row([1, "x", "12.50", "usd", "weekly"])


def test_parse_expense_row_invalid_payment_day_rejected():
    with pytest.raises(ValueError):
        parse_expense_row([1, "x", "12.50", "usd", "monthly", "0"])
    with pytest.raises(ValueError):
        parse_expense_row([1, "x", "12.50", "usd", "monthly", "32"])


def test_parse_expense_row_invalid_boolean_rejected():
    with pytest.raises(ValueError):
        parse_expense_row([1, "x", "12.50", "usd", "monthly", "15", "", "", "maybe"])


def test_parse_expense_row_negative_reminder_days_rejected():
    with pytest.raises(ValueError):
        parse_expense_row([1, "", "", "", "", "", "", "", "", "-1"])
