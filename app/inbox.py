"""Inbox message intake and processing state.

``InboxMessage`` records are created idempotently by ``(source, external_id)``
so re-importing the same external message (e.g. a Gmail message) never produces
duplicates. The state transitions mirror ``app/job_runs.py``: only the allowed
moves are permitted, and every function ``flush``es but never ``commit``s — the
orchestration layer (``app/inbox_processing.py``) owns the commits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import utcnow
from app.models.inbox import InboxMessage, InboxStatus


async def create_message(
    session: AsyncSession,
    *,
    source: str,
    external_id: str,
    sender: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    received_at: datetime | None = None,
) -> InboxMessage:
    """Idempotently create an inbox message keyed by ``(source, external_id)``.

    Returns the existing message unchanged if one is already present, so
    duplicate imports collapse to a single row.
    """

    existing = await _find_by_external_id(session, source, external_id)
    if existing is not None:
        return existing

    message = InboxMessage(
        source=source,
        external_id=external_id,
        sender=sender,
        subject=subject,
        body=body,
        received_at=received_at,
        status=InboxStatus.NEW,
    )
    session.add(message)
    await session.flush()
    return message


async def _find_by_external_id(
    session: AsyncSession, source: str, external_id: str
) -> InboxMessage | None:
    stmt = select(InboxMessage).where(
        InboxMessage.source == source,
        InboxMessage.external_id == external_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_by_external_id(
    session: AsyncSession, source: str, external_id: str
) -> InboxMessage | None:
    """Return the message for ``(source, external_id)``, or ``None``."""

    return await _find_by_external_id(session, source, external_id)


async def get_message(
    session: AsyncSession, message_id: int
) -> InboxMessage | None:
    """Return a message by primary key, or ``None`` if it does not exist."""

    return await session.get(InboxMessage, message_id)


async def list_unprocessed(
    session: AsyncSession, *, limit: int | None = None
) -> list[InboxMessage]:
    """Return ``new`` messages (oldest first), optionally limited."""

    stmt = (
        select(InboxMessage)
        .where(InboxMessage.status == InboxStatus.NEW)
        .order_by(InboxMessage.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_failed(
    session: AsyncSession, *, limit: int | None = None
) -> list[InboxMessage]:
    """Return ``failed`` messages (for reprocessing), optionally limited."""

    stmt = (
        select(InboxMessage)
        .where(InboxMessage.status == InboxStatus.FAILED)
        .order_by(InboxMessage.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def mark_processing(
    session: AsyncSession, message: InboxMessage
) -> InboxMessage:
    """Transition ``new`` or ``failed`` -> ``processing``."""

    if message.status not in (InboxStatus.NEW, InboxStatus.FAILED):
        raise ValueError(
            f"Invalid status transition: {message.status.value} -> processing"
        )
    message.status = InboxStatus.PROCESSING
    await session.flush()
    return message


async def mark_processed(
    session: AsyncSession,
    message: InboxMessage,
    *,
    classification: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> InboxMessage:
    """Transition ``processing`` -> ``processed`` and record the result."""

    if message.status is not InboxStatus.PROCESSING:
        raise ValueError(
            f"Invalid status transition: {message.status.value} -> processed"
        )
    message.status = InboxStatus.PROCESSED
    message.processed_at = utcnow()
    if classification is not None:
        message.classification = classification
    if metadata is not None:
        message.metadata_ = metadata
    await session.flush()
    return message


async def mark_failed(
    session: AsyncSession,
    message: InboxMessage,
    *,
    error: str | None = None,
) -> InboxMessage:
    """Transition ``processing`` -> ``failed`` and record the error."""

    if message.status is not InboxStatus.PROCESSING:
        raise ValueError(
            f"Invalid status transition: {message.status.value} -> failed"
        )
    message.status = InboxStatus.FAILED
    if error is not None:
        message.metadata_ = {"error": error}
    await session.flush()
    return message
