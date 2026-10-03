"""Links the Inbox intake layer with the DeepSeek classifier.

This is the only place that turns an :class:`InboxMessage` into a DeepSeek
classification and persists the result. The LLM never touches the database:
Python reads the message, calls the classifier, validates the result via
Pydantic, and writes it back — and Python (not the LLM) decides whether the
validated result is important enough to notify the user.

Transaction note: a DeepSeek call is a network operation and must not hold an
open DB transaction, so this service owns the commits around the external call
(mirroring ``app/scheduler.py``). Callers should not have unrelated pending
changes on the session they pass here.

Reliability: every attempt is counted; a failed message is retried with a
backoff up to ``MAX_ATTEMPTS`` times, a message left ``processing`` by a crash
is recovered, and the important-email notification is queued in the same
transaction that stores the classification, so it is neither lost nor
duplicated when a message is classified again.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import inbox, notification_outbox
from app.db import utcnow
from app.deepseek.client import DeepSeekClient, DeepSeekError
from app.deepseek.schemas import Classification
from app.models.inbox import InboxMessage

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# Delay before retry n (after n failed attempts): 1 min, 5 min, 15 min, 1 h.
_RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15))
_RETRY_MAX = timedelta(hours=1)
# A ``processing`` attempt older than this was abandoned by a crash.
PROCESSING_TIMEOUT = timedelta(minutes=10)

IMPORTANT_KIND = "gmail_important"
UNCLASSIFIED_KIND = "inbox_unclassified"


def is_important(result: Classification) -> bool:
    """Deterministic rule (Python, not the LLM) for notifying the user."""

    return result.importance == "high" or result.action_required


def _retry_delay(attempt_count: int) -> timedelta:
    index = attempt_count - 1
    return _RETRY_DELAYS[index] if 0 <= index < len(_RETRY_DELAYS) else _RETRY_MAX


def _header(message: InboxMessage) -> str:
    lines = []
    if message.sender:
        lines.append(f"От: {message.sender}")
    lines.append(f"Тема: {message.subject or '(без темы)'}")
    return "\n".join(lines)


def _important_text(message: InboxMessage, result: Classification) -> str:
    text = f"📧 Важное письмо\n\n{_header(message)}\n\n{result.summary}"
    if result.action_required:
        text += "\n\n❗ Требует действия"
    return text


def _unclassified_text(message: InboxMessage, error: str) -> str:
    return (
        "⚠️ Письмо не удалось классифицировать\n\n"
        f"{_header(message)}\n\n"
        f"Попыток: {message.attempt_count}. Последняя ошибка: {error}\n\n"
        "Проверь письмо вручную."
    )


def _dedup_key(prefix: str, message: InboxMessage) -> str:
    return f"{message.source}-{prefix}:{message.external_id}"


async def process_message(
    session: AsyncSession,
    message: InboxMessage,
    deepseek: DeepSeekClient,
) -> InboxMessage:
    """Classify one message and persist the result (plus a notification if needed).

    The message is first marked ``processing`` (committed so the attempt is
    durable before the network call), then classified. On success it becomes
    ``processed`` and, if important, a notification is queued in the same
    transaction. On a DeepSeek error it becomes ``failed`` with a retry time;
    after the last allowed attempt the user is alerted instead.
    """

    await inbox.mark_processing(session, message)
    await session.commit()

    try:
        result = await deepseek.classify_message(
            subject=message.subject, body=message.body
        )
    except DeepSeekError as exc:
        await _record_failure(session, message, str(exc))
        return message

    await inbox.mark_processed(
        session,
        message,
        classification=result.category,
        metadata=result.model_dump(),
    )
    if is_important(result):
        await notification_outbox.enqueue(
            session,
            kind=IMPORTANT_KIND,
            dedup_key=_dedup_key("important", message),
            text=_important_text(message, result),
            source_ref=f"inbox_message:{message.id}",
        )
    await session.commit()
    return message


async def _record_failure(session: AsyncSession, message: InboxMessage, error: str) -> None:
    exhausted = message.attempt_count >= MAX_ATTEMPTS
    next_attempt_at = None if exhausted else utcnow() + _retry_delay(message.attempt_count)
    logger.warning(
        "Classification failed for inbox message %s (attempt %d/%d): %s",
        message.id, message.attempt_count, MAX_ATTEMPTS, error,
    )
    await inbox.mark_failed(session, message, error=error, next_attempt_at=next_attempt_at)
    if exhausted:
        await notification_outbox.enqueue(
            session,
            kind=UNCLASSIFIED_KIND,
            dedup_key=_dedup_key("unclassified", message),
            text=_unclassified_text(message, error),
            source_ref=f"inbox_message:{message.id}",
        )
    await session.commit()


async def process_unprocessed(
    session: AsyncSession,
    deepseek: DeepSeekClient,
    *,
    limit: int | None = None,
) -> list[InboxMessage]:
    """Recover abandoned attempts, then classify new and due-for-retry messages."""

    recovered = await inbox.recover_stale_processing(session, timeout=PROCESSING_TIMEOUT)
    for message in recovered:
        if message.attempt_count >= MAX_ATTEMPTS:  # crashed during its last attempt
            await notification_outbox.enqueue(
                session,
                kind=UNCLASSIFIED_KIND,
                dedup_key=_dedup_key("unclassified", message),
                text=_unclassified_text(message, message.metadata_["error"]),
                source_ref=f"inbox_message:{message.id}",
            )
    await session.commit()
    if recovered:
        logger.warning("Recovered %d abandoned classification attempt(s)", len(recovered))

    messages = await inbox.list_due_for_classification(
        session, max_attempts=MAX_ATTEMPTS, limit=limit
    )
    processed: list[InboxMessage] = []
    for message in messages:
        processed.append(await process_message(session, message, deepseek))
    return processed
