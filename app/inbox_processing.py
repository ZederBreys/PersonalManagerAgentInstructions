"""Links the Inbox intake layer with the DeepSeek classifier.

This is the only place that turns an :class:`InboxMessage` into a DeepSeek
classification and persists the result. The LLM never touches the database:
Python reads the message, calls the classifier, validates the result via
Pydantic, and writes it back.

Transaction note: a DeepSeek call is a network operation and must not hold an
open DB transaction, so this service owns the commits around the external call
(mirroring ``app/scheduler.py``). Callers should not have unrelated pending
changes on the session they pass here.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app import inbox
from app.deepseek.client import DeepSeekClient, DeepSeekError
from app.models.inbox import InboxMessage

logger = logging.getLogger(__name__)


async def process_message(
    session: AsyncSession,
    message: InboxMessage,
    deepseek: DeepSeekClient,
) -> InboxMessage:
    """Classify one message and persist the result.

    The message is first marked ``processing`` (committed so the state is
    durable before the network call), then classified. On success it becomes
    ``processed`` with ``classification``/``metadata``; on a DeepSeek error it
    becomes ``failed`` and is never marked processed.
    """

    await inbox.mark_processing(session, message)
    await session.commit()

    try:
        result = await deepseek.classify_message(
            subject=message.subject, body=message.body
        )
    except DeepSeekError as exc:
        logger.warning(
            "Classification failed for inbox message %s: %s", message.id, exc
        )
        await inbox.mark_failed(session, message, error=str(exc))
        await session.commit()
        return message

    await inbox.mark_processed(
        session,
        message,
        classification=result.category,
        metadata=result.model_dump(),
    )
    await session.commit()
    return message


async def process_unprocessed(
    session: AsyncSession,
    deepseek: DeepSeekClient,
    *,
    limit: int | None = None,
) -> list[InboxMessage]:
    """Process all currently ``new`` inbox messages and return them."""

    messages = await inbox.list_unprocessed(session, limit=limit)
    processed: list[InboxMessage] = []
    for message in messages:
        processed.append(await process_message(session, message, deepseek))
    return processed
