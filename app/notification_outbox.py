"""Notification outbox: durable, at-least-once delivery of user notifications.

Producers call :func:`enqueue` inside the same transaction as the state change
that makes the notification necessary, so "the event happened" and "the user
will be told" are committed atomically. :func:`deliver_due` then sends them:

    claim (status=sending, attempt_count+1) -> COMMIT
    -> Telegram sendMessage (no DB transaction open)
    -> COMMIT delivered | back to pending with last_error and a backoff

Only a successful Bot API response marks a notification delivered. A crash
between Telegram's success and that COMMIT leaves the row ``sending``; it is
recovered as abandoned and sent again — delivery is at-least-once, never
exactly-once (the Bot API has no idempotency key).

Like the rest of the domain layer, every function except :func:`deliver_due`
only flushes; :func:`deliver_due` owns its commits around the network call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.db import utcnow
from app.models.notification import Notification, NotificationStatus
from app.telegram.client import TelegramAPIError, TelegramClient

logger = logging.getLogger(__name__)

# Telegram rejects longer messages; cut instead of failing forever.
MAX_TEXT_LENGTH = 4096
# A ``sending`` claim older than this is treated as abandoned (process died).
SENDING_TIMEOUT = timedelta(minutes=5)
# Retry delay after the n-th failed attempt: 10 s, 30 s, 90 s, ... capped at 1 h.
RETRY_BASE = timedelta(seconds=10)
RETRY_MAX = timedelta(hours=1)


def retry_delay(attempt_count: int) -> timedelta:
    """Bounded exponential backoff after ``attempt_count`` failed attempts."""

    exponent = max(attempt_count - 1, 0)
    return min(RETRY_BASE * (3 ** min(exponent, 10)), RETRY_MAX)


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[: MAX_TEXT_LENGTH - 1] + "…"


async def get_by_dedup_key(session: AsyncSession, dedup_key: str) -> Notification | None:
    result = await session.execute(
        select(Notification).where(Notification.dedup_key == dedup_key)
    )
    return result.scalar_one_or_none()


async def enqueue(
    session: AsyncSession,
    *,
    kind: str,
    dedup_key: str,
    text: str,
    source_ref: str | None = None,
) -> tuple[Notification, bool]:
    """Queue a notification once per ``dedup_key``; return ``(row, created)``.

    Idempotent and race-safe (``INSERT ... ON CONFLICT DO NOTHING`` on the unique
    key): an existing notification is returned unchanged, whatever its state.
    Flushes only — the caller commits together with its own state change.
    """

    await session.flush()
    result = await session.execute(
        insert(Notification)
        .values(
            kind=kind,
            dedup_key=dedup_key,
            text=_clip(text),
            status=NotificationStatus.PENDING.value,
            attempt_count=0,
            source_ref=source_ref,
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
    )
    notification = await get_by_dedup_key(session, dedup_key)
    assert notification is not None
    return notification, bool(result.rowcount)


async def recover_abandoned(
    session: AsyncSession, *, now: datetime | None = None, timeout: timedelta = SENDING_TIMEOUT
) -> int:
    """Return ``sending`` claims older than ``timeout`` to ``pending`` (crash recovery)."""

    now = now or utcnow()
    result = await session.execute(
        select(Notification).where(
            Notification.status == NotificationStatus.SENDING,
            Notification.last_attempt_at < now - timeout,
        )
    )
    stale = list(result.scalars())
    for notification in stale:
        notification.status = NotificationStatus.PENDING
        notification.next_attempt_at = now
        notification.last_error = (
            "Delivery attempt abandoned (process stopped during sending); "
            "it may already have been delivered"
        )
    if stale:
        await session.flush()
    return len(stale)


async def claim_next(
    session: AsyncSession, *, now: datetime | None = None
) -> Notification | None:
    """Claim the oldest due ``pending`` notification and count the attempt.

    The claim is a conditional UPDATE (``WHERE status = 'pending'``), so even
    two processes can never both claim the same row.
    """

    now = now or utcnow()
    while True:
        result = await session.execute(
            select(Notification.id)
            .where(
                Notification.status == NotificationStatus.PENDING,
                (Notification.next_attempt_at.is_(None)) | (Notification.next_attempt_at <= now),
            )
            .order_by(Notification.created_at, Notification.id)
            .limit(1)
        )
        notification_id = result.scalar_one_or_none()
        if notification_id is None:
            return None
        claimed = await session.execute(
            update(Notification)
            .where(Notification.id == notification_id, Notification.status == NotificationStatus.PENDING)
            .values(
                status=NotificationStatus.SENDING,
                attempt_count=Notification.attempt_count + 1,
                last_attempt_at=now,
            )
        )
        if claimed.rowcount:
            notification = await session.get(Notification, notification_id, populate_existing=True)
            return notification
        # Someone else claimed it between the SELECT and the UPDATE: try the next one.


async def finish_attempt(
    session: AsyncSession,
    notification_id: int,
    *,
    attempt: int,
    error: str | None,
    now: datetime | None = None,
) -> bool:
    """Record the outcome of claimed attempt ``attempt``; return False if it is stale.

    The UPDATE only applies while the row is still ``sending`` *for this attempt*.
    If the attempt was meanwhile recovered as abandoned and re-claimed (or
    already finished), a late result must not overwrite the newer state — e.g.
    turn a delivered notification back into pending and cause a resend.
    """

    now = now or utcnow()
    if error is None:
        values = dict(
            status=NotificationStatus.DELIVERED,
            delivered_at=now,
            next_attempt_at=None,
            last_error=None,
        )
    else:
        values = dict(
            status=NotificationStatus.PENDING,
            last_error=error[:1000],
            next_attempt_at=now + retry_delay(attempt),
        )
    result = await session.execute(
        update(Notification)
        .where(
            Notification.id == notification_id,
            Notification.status == NotificationStatus.SENDING,
            Notification.attempt_count == attempt,
        )
        .values(**values)
    )
    return bool(result.rowcount)


def _describe(exc: TelegramAPIError) -> str:
    parts = [exc.description]
    if exc.error_code is not None:
        parts.append(f"error_code={exc.error_code}")
    if exc.http_status is not None:
        parts.append(f"http_status={exc.http_status}")
    return "Telegram: " + ", ".join(parts)


async def deliver_due(
    telegram: TelegramClient, chat_id: int, *, limit: int = 20
) -> tuple[int, int]:
    """Send up to ``limit`` due notifications; return ``(delivered, failed)``.

    Each notification is claimed and finalized in its own short transaction;
    no transaction is open while Telegram is called. After the first failure
    the run stops (Telegram is likely unavailable) and everything else stays
    pending for the next run.
    """

    async with db.get_session() as session:
        recovered = await recover_abandoned(session)
        await session.commit()
    if recovered:
        logger.warning("Recovered %d abandoned notification delivery attempt(s)", recovered)

    delivered = failed = 0
    for _ in range(limit):
        async with db.get_session() as session:
            notification = await claim_next(session)
            await session.commit()
        if notification is None:
            break

        error: str | None = None
        try:
            result = await telegram.send_message(chat_id=chat_id, text=notification.text)
            if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
                error = "Telegram: unexpected sendMessage result (no message_id)"
        except TelegramAPIError as exc:
            error = _describe(exc)

        async with db.get_session() as session:
            recorded = await finish_attempt(
                session, notification.id, attempt=notification.attempt_count, error=error
            )
            await session.commit()
        if not recorded:
            logger.warning(
                "Outcome of notification %s attempt %d ignored: the attempt was "
                "already recovered or finished elsewhere",
                notification.id, notification.attempt_count,
            )

        if error is None:
            delivered += 1
            continue
        failed += 1
        logger.warning(
            "Notification %s (%s) not delivered, attempt %d: %s",
            notification.id, notification.kind, notification.attempt_count, error,
        )
        break
    return delivered, failed


async def queue_summary(session: AsyncSession) -> dict[str, int]:
    """Count notifications per status (for ``/status``)."""

    result = await session.execute(
        select(Notification.status, func.count()).group_by(Notification.status)
    )
    return {status.value: count for status, count in result.all()}
