"""Notification outbox: creation/dedup, delivery, retries, crash recovery,
important Gmail and payment reminders (fake Telegram/DeepSeek, real SQLite)."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app import db, expenses, inbox, notification_outbox
from app.config import Settings
from app.db import utcnow
from app.deepseek import Classification, DeepSeekError
from app.inbox_processing import MAX_ATTEMPTS, process_message, process_unprocessed
from app.jobs import Services, build_jobs, daily_reminders, deliver_notifications
from app.models.inbox import InboxMessage, InboxStatus
from app.models.notification import Notification, NotificationStatus
from app.telegram import bot
from app.telegram.client import TelegramAPIError

CHAT_ID = 777


class FakeTelegram:
    def __init__(self, *, fail: bool = False, result: object = "ok") -> None:
        self.fail = fail
        self.result = result
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str):
        if self.fail:
            raise TelegramAPIError(description="Bad Gateway", error_code=502, http_status=502)
        self.sent.append(text)
        if self.result != "ok":
            return self.result
        return {"message_id": len(self.sent)}


class FakeDeepSeek:
    def __init__(self, result: Classification | None = None, error: Exception | None = None):
        self.result = result or Classification(
            category="financial", importance="high", summary="Счёт к оплате", action_required=True
        )
        self.error = error
        self.calls = 0

    async def classify_message(self, *, subject=None, body=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _run(coro):
    return asyncio.run(coro)


async def _all_notifications() -> list[Notification]:
    async with db.get_session() as session:
        result = await session.execute(select(Notification).order_by(Notification.id))
        return list(result.scalars())


async def _enqueue(key: str = "k1", text: str = "hello") -> int:
    async with db.get_session() as session:
        notification, _ = await notification_outbox.enqueue(
            session, kind="test", dedup_key=key, text=text
        )
        await session.commit()
        return notification.id


async def _update(notification_id: int, **values) -> None:
    async with db.get_session() as session:
        row = await session.get(Notification, notification_id)
        for name, value in values.items():
            setattr(row, name, value)
        await session.commit()


async def _get(notification_id: int) -> Notification:
    async with db.get_session() as session:
        return await session.get(Notification, notification_id)


def _deliver(telegram: FakeTelegram, limit: int = 20) -> tuple[int, int]:
    return _run(notification_outbox.deliver_due(telegram, CHAT_ID, limit=limit))  # type: ignore[arg-type]


# --- creation / deduplication ------------------------------------------------------


def test_enqueue_is_idempotent_by_dedup_key(schema: None) -> None:
    async def _r() -> None:
        async with db.get_session() as session:
            first, created1 = await notification_outbox.enqueue(
                session, kind="t", dedup_key="same", text="one"
            )
            second, created2 = await notification_outbox.enqueue(
                session, kind="t", dedup_key="same", text="two"
            )
            await session.commit()
        assert (created1, created2) == (True, False)
        assert first.id == second.id
        [row] = await _all_notifications()
        assert row.text == "one" and row.status is NotificationStatus.PENDING

    _run(_r())


def test_enqueue_does_not_reset_delivered_notification(schema: None) -> None:
    notification_id = _run(_enqueue("k"))
    _deliver(FakeTelegram())
    _run(_enqueue("k"))
    assert _run(_get(notification_id)).status is NotificationStatus.DELIVERED
    assert len(_run(_all_notifications())) == 1


def test_long_text_is_clipped_to_telegram_limit(schema: None) -> None:
    notification_id = _run(_enqueue(text="x" * 5000))
    assert len(_run(_get(notification_id)).text) == notification_outbox.MAX_TEXT_LENGTH


# --- delivery -------------------------------------------------------------------------


def test_pending_is_delivered_once(schema: None) -> None:
    notification_id = _run(_enqueue())
    telegram = FakeTelegram()

    assert _deliver(telegram) == (1, 0)
    row = _run(_get(notification_id))
    assert row.status is NotificationStatus.DELIVERED
    assert row.delivered_at is not None and row.attempt_count == 1 and row.last_error is None

    assert _deliver(telegram) == (0, 0)  # delivered is never sent again
    assert telegram.sent == ["hello"]


def test_failure_keeps_notification_retryable_with_error_and_backoff(schema: None) -> None:
    notification_id = _run(_enqueue())

    assert _deliver(FakeTelegram(fail=True)) == (0, 1)
    row = _run(_get(notification_id))
    assert row.status is NotificationStatus.PENDING
    assert row.attempt_count == 1
    assert "Bad Gateway" in row.last_error and "502" in row.last_error
    assert row.next_attempt_at > utcnow()

    telegram = FakeTelegram()
    assert _deliver(telegram) == (0, 0)  # backoff not elapsed yet: not resent
    _run(_update(notification_id, next_attempt_at=utcnow() - timedelta(seconds=1)))
    assert _deliver(telegram) == (1, 0)  # the same row is retried, no new notification
    row = _run(_get(notification_id))
    assert row.status is NotificationStatus.DELIVERED and row.attempt_count == 2
    assert len(_run(_all_notifications())) == 1


def test_response_without_message_id_is_not_delivered(schema: None) -> None:
    notification_id = _run(_enqueue())
    assert _deliver(FakeTelegram(result={})) == (0, 1)
    row = _run(_get(notification_id))
    assert row.status is NotificationStatus.PENDING and "message_id" in row.last_error


def test_run_stops_after_first_failure(schema: None) -> None:
    first = _run(_enqueue("a"))
    second = _run(_enqueue("b"))
    _deliver(FakeTelegram(fail=True))
    assert _run(_get(first)).attempt_count == 1
    assert _run(_get(second)).attempt_count == 0  # not hammered while Telegram is down


def test_batch_limit(schema: None) -> None:
    for key in "abc":
        _run(_enqueue(key))
    assert _deliver(FakeTelegram(), limit=2) == (2, 0)
    assert _deliver(FakeTelegram(), limit=2) == (1, 0)


def test_retry_delay_is_bounded_exponential() -> None:
    delays = [notification_outbox.retry_delay(n) for n in range(1, 12)]
    assert delays[:3] == [timedelta(seconds=10), timedelta(seconds=30), timedelta(seconds=90)]
    assert delays == sorted(delays)
    assert max(delays) == notification_outbox.RETRY_MAX


# --- crash recovery -------------------------------------------------------------------


def test_abandoned_sending_claim_is_recovered_and_sent(schema: None) -> None:
    notification_id = _run(_enqueue())
    stale = utcnow() - notification_outbox.SENDING_TIMEOUT - timedelta(minutes=1)
    # The process died after claiming (or after Telegram accepted, before COMMIT).
    _run(_update(notification_id, status=NotificationStatus.SENDING, attempt_count=1, last_attempt_at=stale))

    telegram = FakeTelegram()
    assert _deliver(telegram) == (1, 0)  # at-least-once: sent again after recovery
    row = _run(_get(notification_id))
    assert row.status is NotificationStatus.DELIVERED and row.attempt_count == 2


def test_fresh_sending_claim_is_not_stolen(schema: None) -> None:
    notification_id = _run(_enqueue())
    _run(_update(notification_id, status=NotificationStatus.SENDING, last_attempt_at=utcnow()))
    assert _deliver(FakeTelegram()) == (0, 0)
    assert _run(_get(notification_id)).status is NotificationStatus.SENDING


def test_pending_survives_new_services_and_job_run(schema: None) -> None:
    notification_id = _run(_enqueue())
    # A "restart": brand-new services/clients, the job runs again from scratch.
    telegram = FakeTelegram()
    services = Services(settings=Settings(_env_file=None, telegram_chat_id=CHAT_ID), telegram=telegram)  # type: ignore[arg-type]
    _run(deliver_notifications(services))
    assert telegram.sent == ["hello"]
    assert _run(_get(notification_id)).status is NotificationStatus.DELIVERED


def test_delivery_job_is_registered_only_with_telegram() -> None:
    names = lambda services: [j.name for j in build_jobs(services)]  # noqa: E731
    settings = Settings(_env_file=None, telegram_chat_id=CHAT_ID)
    assert "deliver_notifications" in names(Services(settings=settings, telegram=FakeTelegram()))  # type: ignore[arg-type]
    assert "deliver_notifications" not in names(Services(settings=settings))


def test_status_command_shows_queue(schema: None) -> None:
    _run(_enqueue("a"))
    _run(_enqueue("b"))
    _deliver(FakeTelegram(), limit=1)
    text = _run(bot.answer("/status", []))
    assert "в очереди 1" in text and "доставлено 1" in text


# --- important Gmail ------------------------------------------------------------------


async def _message(external_id: str = "gm1") -> int:
    async with db.get_session() as session:
        message = await inbox.create_message(
            session,
            source="gmail",
            external_id=external_id,
            sender="support@liteserver.nl",
            subject="Invoice",
            body="Please pay",
        )
        await session.commit()
        return message.id


async def _inbox(message_id: int) -> InboxMessage:
    async with db.get_session() as session:
        return await session.get(InboxMessage, message_id)


def _process_all(deepseek: FakeDeepSeek) -> None:
    async def _r() -> None:
        async with db.get_session() as session:
            await process_unprocessed(session, deepseek)  # type: ignore[arg-type]

    _run(_r())


def test_important_gmail_creates_one_pending_notification(schema: None) -> None:
    message_id = _run(_message())
    _process_all(FakeDeepSeek())

    [notification] = _run(_all_notifications())
    assert notification.dedup_key == "gmail-important:gm1"
    assert notification.kind == "gmail_important"
    assert notification.status is NotificationStatus.PENDING
    assert "support@liteserver.nl" in notification.text and "Счёт к оплате" in notification.text
    assert notification.source_ref == f"inbox_message:{message_id}"
    assert _run(_inbox(message_id)).status is InboxStatus.PROCESSED


def test_unimportant_gmail_creates_no_notification(schema: None) -> None:
    _run(_message())
    _process_all(
        FakeDeepSeek(
            Classification(
                category="informational", importance="low", summary="FYI", action_required=False
            )
        )
    )
    assert _run(_all_notifications()) == []


def test_reclassification_does_not_duplicate_notification(schema: None) -> None:
    message_id = _run(_message())
    _process_all(FakeDeepSeek())

    async def classify_again() -> None:
        async with db.get_session() as session:
            message = await session.get(InboxMessage, message_id)
            message.status = InboxStatus.FAILED  # e.g. retried after an ambiguous crash
            await session.commit()
            await process_message(session, message, FakeDeepSeek())  # type: ignore[arg-type]

    _run(classify_again())
    assert len(_run(_all_notifications())) == 1


def test_classification_failure_is_retried_after_backoff(schema: None) -> None:
    message_id = _run(_message())
    _process_all(FakeDeepSeek(error=DeepSeekError(message="timeout", http_status=504)))

    failed = _run(_inbox(message_id))
    assert failed.status is InboxStatus.FAILED
    assert failed.attempt_count == 1 and failed.last_attempt_at is not None
    assert failed.metadata_ == {"error": "timeout"}
    assert failed.next_attempt_at > utcnow()
    assert _run(_all_notifications()) == []

    deepseek = FakeDeepSeek()
    _process_all(deepseek)
    assert deepseek.calls == 0  # retry not due yet

    async def make_due() -> None:
        async with db.get_session() as session:
            message = await session.get(InboxMessage, message_id)
            message.next_attempt_at = utcnow() - timedelta(seconds=1)
            await session.commit()

    _run(make_due())
    _process_all(deepseek)
    done = _run(_inbox(message_id))
    assert done.status is InboxStatus.PROCESSED and done.attempt_count == 2
    assert len(_run(_all_notifications())) == 1


def test_exhausted_classification_alerts_once_and_stops(schema: None) -> None:
    message_id = _run(_message())
    deepseek = FakeDeepSeek(error=DeepSeekError(message="Invalid JSON from model"))

    async def fail_until_exhausted() -> None:
        for _ in range(MAX_ATTEMPTS + 2):
            async with db.get_session() as session:
                message = await session.get(InboxMessage, message_id)
                if message.next_attempt_at is not None:
                    message.next_attempt_at = utcnow() - timedelta(seconds=1)
                    await session.commit()
                await process_unprocessed(session, deepseek)  # type: ignore[arg-type]

    _run(fail_until_exhausted())
    assert deepseek.calls == MAX_ATTEMPTS
    message = _run(_inbox(message_id))
    assert message.status is InboxStatus.FAILED and message.attempt_count == MAX_ATTEMPTS
    [alert] = _run(_all_notifications())
    assert alert.dedup_key == "gmail-unclassified:gm1"
    assert "Invalid JSON from model" in alert.text


@pytest.mark.parametrize("last_attempt", ["stale", "legacy_null"])
def test_stale_processing_is_recovered_and_classified(schema: None, last_attempt: str) -> None:
    message_id = _run(_message())

    async def crash_mid_classification() -> None:
        async with db.get_session() as session:
            message = await session.get(InboxMessage, message_id)
            await inbox.mark_processing(session, message)
            message.last_attempt_at = (
                None if last_attempt == "legacy_null" else utcnow() - timedelta(hours=1)
            )
            await session.commit()

    _run(crash_mid_classification())
    _process_all(FakeDeepSeek())
    assert _run(_inbox(message_id)).status is InboxStatus.PROCESSED
    assert len(_run(_all_notifications())) == 1


def test_fresh_processing_is_left_alone(schema: None) -> None:
    message_id = _run(_message())

    async def start() -> None:
        async with db.get_session() as session:
            await inbox.mark_processing(session, await session.get(InboxMessage, message_id))
            await session.commit()

    _run(start())
    deepseek = FakeDeepSeek()
    _process_all(deepseek)
    assert deepseek.calls == 0
    assert _run(_inbox(message_id)).status is InboxStatus.PROCESSING


def test_important_gmail_end_to_end_delivery(schema: None) -> None:
    _run(_message())
    _process_all(FakeDeepSeek())
    telegram = FakeTelegram()
    assert _deliver(telegram) == (1, 0)
    assert "Важное письмо" in telegram.sent[0]
    _process_all(FakeDeepSeek())  # nothing left to classify, nothing new queued
    assert _deliver(telegram) == (0, 0)


# --- payment reminders ----------------------------------------------------------------


async def _internet(**kwargs) -> int:
    async with db.get_session() as session:
        values = dict(
            name="Internet",
            amount_minor=100000,
            currency="RUB",
            period="monthly",
            payment_day=15,
            next_payment_date=date(2026, 10, 15),
            reminder_days_before=3,
        )
        values.update(kwargs)
        expense = await expenses.create_expense(session, **values)
        await session.commit()
        return expense.id


def _daily(today: date) -> None:
    _run(daily_reminders(Services(settings=Settings(_env_file=None)), today=today))


def _payment_notifications() -> list[Notification]:
    return [n for n in _run(_all_notifications()) if n.kind == "payment_reminder"]


def test_payment_reminder_created_three_days_before(schema: None) -> None:
    expense_id = _run(_internet())

    _daily(date(2026, 10, 11))
    assert _payment_notifications() == []

    _daily(date(2026, 10, 12))
    [reminder] = _payment_notifications()
    assert reminder.dedup_key == f"payment-reminder:{expense_id}:2026-10-15:3"
    assert "Internet — 1000.00 RUB" in reminder.text
    assert "15.10.2026 (через 3 дн.)" in reminder.text


def test_payment_reminder_not_duplicated_same_day_or_later_in_window(schema: None) -> None:
    _run(_internet())
    _daily(date(2026, 10, 12))
    _daily(date(2026, 10, 12))  # rerun / restart the same day
    _daily(date(2026, 10, 14))
    _daily(date(2026, 10, 15))
    assert len(_payment_notifications()) == 1


def test_payment_reminder_missed_day_is_caught_up_within_window(schema: None) -> None:
    _run(_internet())
    _daily(date(2026, 10, 14))  # the app was down on the 12th and 13th
    [reminder] = _payment_notifications()
    assert "через 1 дн." in reminder.text


def test_next_payment_cycle_creates_new_reminder(schema: None) -> None:
    expense_id = _run(_internet())
    _daily(date(2026, 10, 12))
    _daily(date(2026, 10, 16))  # payment passed: the expense advances to 2026-11-15
    assert len(_payment_notifications()) == 1

    _daily(date(2026, 11, 12))
    keys = [n.dedup_key for n in _payment_notifications()]
    assert keys == [
        f"payment-reminder:{expense_id}:2026-10-15:3",
        f"payment-reminder:{expense_id}:2026-11-15:3",
    ]


def test_payment_reminder_on_the_day_and_inactive(schema: None) -> None:
    _run(_internet(name="Same day", reminder_days_before=0))
    _run(_internet(name="Paused", is_active=False))
    _daily(date(2026, 10, 14))
    assert _payment_notifications() == []
    _daily(date(2026, 10, 15))
    [reminder] = _payment_notifications()
    assert "Same day" in reminder.text and "(сегодня)" in reminder.text


def test_payment_reminder_delivered_through_outbox(schema: None) -> None:
    _run(_internet())
    _daily(date(2026, 10, 12))
    telegram = FakeTelegram()
    assert _deliver(telegram) == (1, 0)
    assert "Предстоящий платёж" in telegram.sent[0]
    _daily(date(2026, 10, 13))
    assert _deliver(telegram) == (0, 0)


def test_negative_reminder_days_rejected(schema: None) -> None:
    with pytest.raises(ValueError):
        _run(_internet(reminder_days_before=-1))
