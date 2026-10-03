"""Turn due event reminders and upcoming payments into outbox notifications.

Nothing here talks to Telegram: notifications are queued in the outbox inside
the caller's transaction and delivered later by ``notification_outbox``.
Each queued item has a deterministic deduplication key, so running the daily
job again (same day, after a restart, after a Telegram outage) never queues
the same reminder twice:

* event reminder:   ``event-reminder:{reminder_id}``
* payment reminder: ``payment-reminder:{expense_id}:{payment_date}:{days_before}``
  — one per expense *payment cycle*; the next cycle has a new payment date.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import expenses, notification_outbox, reminders
from app.google_sheets.mappers import minor_to_display
from app.models.event import Event
from app.models.expense import RecurringExpense
from app.models.reminder import Reminder

EVENT_REMINDER_KIND = "event_reminder"
PAYMENT_REMINDER_KIND = "payment_reminder"


def _when(target: date, today: date) -> str:
    days_left = (target - today).days
    if days_left > 0:
        return f"через {days_left} дн."
    if days_left == 0:
        return "сегодня"
    return "уже прошло"


def event_reminder_text(event: Event, today: date) -> str:
    lines = [
        "🔔 Напоминание",
        "",
        event.name,
        "",
        f"Дата: {event.next_date.strftime('%d.%m.%Y')} ({_when(event.next_date, today)})",
    ]
    if event.action_text:
        lines += ["", event.action_text]
    return "\n".join(lines)


def payment_reminder_text(expense: RecurringExpense, today: date) -> str:
    payment_date = expense.next_payment_date
    lines = [
        "💳 Предстоящий платёж",
        "",
        f"{expense.name} — {minor_to_display(expense.amount_minor)} {expense.currency}",
        f"Дата: {payment_date.strftime('%d.%m.%Y')} ({_when(payment_date, today)})",
    ]
    if expense.category:
        lines.append(f"Категория: {expense.category}")
    return "\n".join(lines)


def payment_reminder_key(expense: RecurringExpense) -> str:
    return (
        f"payment-reminder:{expense.id}:{expense.next_payment_date.isoformat()}"
        f":{expense.reminder_days_before}"
    )


def payment_reminder_due(expense: RecurringExpense, today: date) -> bool:
    """True from ``days_before`` days ahead until the payment day itself.

    The window (not a single day) means a reminder missed while the app was
    down on the exact day is still queued later, as long as it is not too late.
    """

    if not expense.is_active or expense.next_payment_date is None:
        return False
    remind_from = expense.next_payment_date - timedelta(days=expense.reminder_days_before)
    return remind_from <= today <= expense.next_payment_date


async def queue_event_reminders(session: AsyncSession, today: date) -> int:
    """Queue due reminders of active events and mark them sent (handed to the outbox)."""

    queued = 0
    for reminder in await reminders.get_due_reminders(session, today=today):
        event = await session.get(Event, reminder.event_id)
        if event is None or not event.is_active:
            continue
        await _queue_event_reminder(session, reminder, event, today)
        queued += 1
    return queued


async def _queue_event_reminder(
    session: AsyncSession, reminder: Reminder, event: Event, today: date
) -> None:
    await notification_outbox.enqueue(
        session,
        kind=EVENT_REMINDER_KIND,
        dedup_key=f"event-reminder:{reminder.id}",
        text=event_reminder_text(event, today),
        source_ref=f"reminder:{reminder.id}",
    )
    # Same transaction as the enqueue: the reminder is never queued twice and
    # never marked sent without its notification. Delivery is tracked in the outbox.
    await reminders.mark_sent(session, reminder)


async def queue_payment_reminders(session: AsyncSession, today: date) -> int:
    """Queue one reminder per due payment cycle; return how many were new."""

    created = 0
    for expense in await expenses.list_expenses(session, active_only=True):
        if not payment_reminder_due(expense, today):
            continue
        _, is_new = await notification_outbox.enqueue(
            session,
            kind=PAYMENT_REMINDER_KIND,
            dedup_key=payment_reminder_key(expense),
            text=payment_reminder_text(expense, today),
            source_ref=f"expense:{expense.id}",
        )
        created += is_new
    return created
