"""Event / important date domain model."""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Enum, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow

if TYPE_CHECKING:
    from app.models.reminder import Reminder


class EventRecurrence(str, enum.Enum):
    """Recurrence rule for an event.

    Kept deliberately minimal: one-off events and yearly ones cover birthdays
    and anniversaries. Complex RRULE/cron-like rules are out of scope.
    """

    NONE = "none"
    YEARLY = "yearly"


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        # SQLite Enum has no native type and does not auto-emit a CHECK, so
        # enforce allowed recurrence values explicitly.
        CheckConstraint(
            "recurrence IN ('none', 'yearly')", name="ck_events_recurrence"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    next_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Canonical annual anchor (month/day) used to advance a YEARLY event. Kept
    # separate from next_date so a Feb 29 event returns to Feb 29 after a
    # non-leap year instead of drifting to Feb 28 forever. Null for NONE.
    anchor_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Lead times (days before next_date) used to generate reminders. Persisted
    # on the event so the offset configuration survives reminder regeneration
    # and annual advancement. 0 == the day of the event.
    reminder_offsets: Mapped[list[int]] = mapped_column(
        JSON, nullable=False, default=lambda: [0]
    )
    recurrence: Mapped[EventRecurrence] = mapped_column(
        Enum(
            EventRecurrence,
            values_callable=lambda e: [m.value for m in e],
            native_enum=False,
        ),
        nullable=False,
        default=EventRecurrence.NONE,
    )
    action_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # One-time key of the Google Sheets row this event was created from; makes
    # creation from Sheets idempotent (see app.google_sheets.sync).
    sheet_key: Mapped[str | None] = mapped_column(
        String(32), nullable=True, unique=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Reminder.remind_at",
    )

    def __repr__(self) -> str:
        return f"<Event id={self.id} name={self.name!r} next_date={self.next_date}>"
