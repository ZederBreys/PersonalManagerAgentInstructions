"""Deterministic domain operations for Reminders.

Reminders are derived state: the Event is the source of truth for its date.
A Reminder is a concrete reminder instance tied to an Event's current
``next_date``.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import utcnow
from app.models.event import Event
from app.models.reminder import Reminder

# Lead times (days before the event) for generated reminders. 0 == the day of
# the event. This is the fallback used when an event has no persisted offsets
# yet; the authoritative value lives on ``Event.reminder_offsets``.
DEFAULT_REMINDER_OFFSETS: tuple[int, ...] = (0,)


def _coerce_offsets(offsets: object) -> list[int]:
    """Normalize a reminder-offset collection to a sorted, de-duplicated list.

    Accepts an int or an iterable of ints. Raises ``ValueError`` for negative
    or non-integer values.
    """

    if isinstance(offsets, int):
        raw: object = (offsets,)
    else:
        try:
            raw = list(offsets)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError(f"Invalid reminder offsets: {offsets!r}") from exc
    try:
        values = [int(v) for v in raw]  # type: ignore[union-attr]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid reminder offsets: {offsets!r}") from exc
    if any(v < 0 for v in values):
        raise ValueError("Reminder offsets must be non-negative days")
    return sorted(set(values))


async def get_reminders_for_event(
    session: AsyncSession, event_id: int
) -> list[Reminder]:
    """Return all reminders for an event, ordered by remind_at."""

    result = await session.execute(
        select(Reminder)
        .where(Reminder.event_id == event_id)
        .order_by(Reminder.remind_at)
    )
    return list(result.scalars().all())


async def list_reminders(
    session: AsyncSession, *, event_id: int | None = None
) -> list[Reminder]:
    """Return reminders ordered by remind_at; optionally filter to one event."""

    stmt = select(Reminder).order_by(Reminder.remind_at, Reminder.id)
    if event_id is not None:
        stmt = stmt.where(Reminder.event_id == event_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def generate_reminders(
    session: AsyncSession,
    event: Event,
    *,
    offsets: tuple[int, ...] | list[int] | None = None,
) -> list[Reminder]:
    """Create pending reminders for ``event``'s current ``next_date``.

    Each offset is a lead time in days before ``next_date`` (0 == the event
    day). Idempotent: a reminder already existing for the same
    ``(event_id, remind_at)`` is skipped, backed by a DB unique constraint.

    When ``offsets`` is omitted, the event's persisted ``reminder_offsets``
    are used. When ``offsets`` is provided, it is normalized and persisted
    back onto the event so future regenerations/advancements keep it.
    """

    if offsets is None:
        resolved = tuple(event.reminder_offsets or DEFAULT_REMINDER_OFFSETS)
    else:
        event.reminder_offsets = _coerce_offsets(offsets)
        resolved = tuple(event.reminder_offsets)

    created: list[Reminder] = []
    existing = {
        reminder.remind_at
        for reminder in await get_reminders_for_event(session, event.id)
    }
    for offset in resolved:
        remind_at = event.next_date - timedelta(days=offset)
        if remind_at in existing:
            continue
        reminder = Reminder(event_id=event.id, remind_at=remind_at)
        session.add(reminder)
        created.append(reminder)
        existing.add(remind_at)
    if created:
        await session.flush()
    return created


async def regenerate_reminders(
    session: AsyncSession,
    event: Event,
    *,
    offsets: tuple[int, ...] | list[int] | None = None,
) -> list[Reminder]:
    """Replace pending reminders for ``event`` with ones for its current date.

    Only reminders that are neither sent nor done are removed and recreated;
    already sent/done reminders are history and are preserved. Uses the event's
    persisted offsets unless ``offsets`` is explicitly provided.
    """

    await session.execute(
        delete(Reminder).where(
            Reminder.event_id == event.id,
            Reminder.is_sent.is_(False),
            Reminder.is_done.is_(False),
        )
    )
    return await generate_reminders(session, event, offsets=offsets)


async def get_pending_reminders(session: AsyncSession) -> list[Reminder]:
    """Return reminders that are neither done nor sent."""

    result = await session.execute(
        select(Reminder)
        .where(Reminder.is_done.is_(False), Reminder.is_sent.is_(False))
        .order_by(Reminder.remind_at)
    )
    return list(result.scalars().all())


async def get_due_reminders(
    session: AsyncSession, *, today: date | None = None
) -> list[Reminder]:
    """Return pending reminders whose remind_at has been reached."""

    today = today or date.today()
    result = await session.execute(
        select(Reminder)
        .where(
            Reminder.is_done.is_(False),
            Reminder.is_sent.is_(False),
            Reminder.remind_at <= today,
        )
        .order_by(Reminder.remind_at)
    )
    return list(result.scalars().all())


async def mark_done(session: AsyncSession, reminder: Reminder) -> Reminder:
    """Mark a reminder as done."""

    reminder.is_done = True
    await session.flush()
    return reminder


async def mark_sent(session: AsyncSession, reminder: Reminder) -> Reminder:
    """Mark a reminder as sent and record the (naive UTC) send time."""

    reminder.is_sent = True
    reminder.sent_at = utcnow()
    await session.flush()
    return reminder
