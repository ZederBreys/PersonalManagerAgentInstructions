"""Notification outbox model: user-facing messages waiting for (or past) delivery.

A row is created *before* anything is sent and is the source of truth for the
delivery state (at-least-once): ``pending`` -> ``sending`` (claimed by the
delivery job) -> ``delivered``, or back to ``pending`` with ``last_error`` and a
later ``next_attempt_at`` when Telegram fails. ``dedup_key`` is unique, so the
same logical notification (one email, one payment cycle, one event reminder)
is never queued twice.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class NotificationStatus(str, enum.Enum):
    PENDING = "pending"
    SENDING = "sending"
    DELIVERED = "delivered"


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sending', 'delivered')",
            name="ck_notifications_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # What produced it, e.g. "gmail_important", "payment_reminder".
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(
            NotificationStatus,
            values_callable=lambda e: [m.value for m in e],
            native_enum=False,
        ),
        nullable=False,
        default=NotificationStatus.PENDING,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Earliest time of the next delivery attempt; NULL means "now".
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Diagnostic pointer to the source record, e.g. "inbox_message:12".
    source_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    def __repr__(self) -> str:
        return (
            f"<Notification id={self.id} kind={self.kind!r} "
            f"status={self.status.value} attempts={self.attempt_count}>"
        )
