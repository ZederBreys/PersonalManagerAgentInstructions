"""Reminder domain model (a concrete reminder tied to an Event)."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow

if TYPE_CHECKING:
    from app.models.event import Event


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        # One reminder per event per calendar date -> idempotent generation.
        UniqueConstraint("event_id", "remind_at", name="uq_reminders_event_id_remind_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    remind_at: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    is_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    event: Mapped["Event"] = relationship(back_populates="reminders")

    def __repr__(self) -> str:
        return f"<Reminder id={self.id} event_id={self.event_id} remind_at={self.remind_at}>"
