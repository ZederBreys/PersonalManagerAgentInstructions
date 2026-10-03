"""Deterministic domain operations for Events.

An Event is the source of truth for its own date; reminders are derived state.
All functions operate on a caller-provided session and do not commit, so the
caller owns the transaction and can atomically update an Event together with
its reminders.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dates import yearly_occurrence
from app.models.event import Event, EventRecurrence
from app.reminders import _coerce_offsets, regenerate_reminders


def _coerce_recurrence(recurrence: object) -> EventRecurrence:
    if isinstance(recurrence, EventRecurrence):
        return recurrence
    try:
        return EventRecurrence(recurrence)  # type: ignore[arg-type]
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Invalid recurrence: {recurrence!r}") from exc


def _clean_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Event name must be a non-empty string")
    return name.strip()


async def create_event(
    session: AsyncSession,
    *,
    name: str,
    next_date: date,
    recurrence: EventRecurrence | str = EventRecurrence.NONE,
    action_text: str | None = None,
    is_active: bool = True,
) -> Event:
    """Create a new Event and add it to the session (caller commits)."""

    rec = _coerce_recurrence(recurrence)
    event = Event(
        name=_clean_name(name),
        next_date=next_date,
        recurrence=rec,
        action_text=action_text,
        is_active=is_active,
    )
    event.anchor_date = next_date if rec is EventRecurrence.YEARLY else None
    session.add(event)
    await session.flush()
    return event


async def get_event(session: AsyncSession, event_id: int) -> Event | None:
    """Return an Event by id, or ``None`` if it does not exist."""

    return await session.get(Event, event_id)


async def list_events(
    session: AsyncSession, *, active_only: bool = True
) -> list[Event]:
    """Return events ordered by next_date; by default only active ones."""

    stmt = select(Event).order_by(Event.next_date, Event.id)
    if active_only:
        stmt = stmt.where(Event.is_active.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_event(
    session: AsyncSession,
    event: Event,
    *,
    name: str | None = None,
    next_date: date | None = None,
    recurrence: EventRecurrence | str | None = None,
    action_text: str | None = None,
    is_active: bool | None = None,
    reminder_offsets: list[int] | tuple[int, ...] | None = None,
) -> Event:
    """Update fields of ``event`` (caller commits).

    When ``next_date``, ``recurrence`` or ``reminder_offsets`` changes, pending
    reminders are regenerated to match; sent/done reminders are kept.
    """

    reminder_affected = False

    if name is not None:
        event.name = _clean_name(name)

    if recurrence is not None:
        rec = _coerce_recurrence(recurrence)
        if rec is not event.recurrence:
            event.recurrence = rec
            reminder_affected = True
        event.anchor_date = event.next_date if rec is EventRecurrence.YEARLY else None

    if next_date is not None:
        if next_date != event.next_date:
            event.next_date = next_date
            reminder_affected = True
        if event.recurrence is EventRecurrence.YEARLY:
            event.anchor_date = next_date

    if action_text is not None:
        event.action_text = action_text

    if is_active is not None:
        event.is_active = is_active

    if reminder_offsets is not None:
        event.reminder_offsets = _coerce_offsets(reminder_offsets)
        reminder_affected = True

    if reminder_affected:
        await regenerate_reminders(session, event)

    await session.flush()
    return event


async def delete_event(session: AsyncSession, event: Event) -> None:
    """Delete an Event; DB-level ON DELETE CASCADE removes its reminders."""

    await session.delete(event)
    await session.flush()


async def advance_event(
    session: AsyncSession, event: Event, *, today: date | None = None
) -> Event:
    """Move a due YEARLY event forward to the next occurrence >= ``today``.

    ``NONE`` events are left untouched. The anchor date is preserved, so a
    Feb 29 event keeps returning to Feb 29 in leap years.
    """

    today = today or date.today()
    if event.recurrence is EventRecurrence.YEARLY and event.next_date < today:
        anchor = event.anchor_date or event.next_date
        event.next_date = yearly_occurrence(anchor, today)
        await regenerate_reminders(session, event)
        await session.flush()
    return event


async def advance_due_events(
    session: AsyncSession, *, today: date | None = None
) -> list[Event]:
    """Advance all active, overdue YEARLY events and return the changed ones."""

    today = today or date.today()
    advanced: list[Event] = []
    for event in await list_events(session, active_only=True):
        if event.recurrence is EventRecurrence.YEARLY and event.next_date < today:
            anchor = event.anchor_date or event.next_date
            event.next_date = yearly_occurrence(anchor, today)
            advanced.append(event)
    for event in advanced:
        await regenerate_reminders(session, event)
    if advanced:
        await session.flush()
    return advanced
