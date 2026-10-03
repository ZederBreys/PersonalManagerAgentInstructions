"""Inbox message model (internal intake of incoming messages).

``InboxMessage`` is the persisted record of a message that arrived from an
external source (e.g. Gmail) and may still need semantic classification. The
``(source, external_id)`` pair is unique so importing the same external message
twice never creates a duplicate.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class InboxStatus(str, enum.Enum):
    """Allowed lifecycle states of an inbox message."""

    NEW = "new"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    __table_args__ = (
        # Idempotent import: one row per external message.
        UniqueConstraint(
            "source", "external_id", name="uq_inbox_messages_source_external_id"
        ),
        # SQLite Enum has no native type and does not auto-emit a CHECK, so
        # enforce the allowed status values explicitly.
        CheckConstraint(
            "status IN ('new', 'processing', 'processed', 'failed')",
            name="ck_inbox_messages_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sender: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[InboxStatus] = mapped_column(
        Enum(
            InboxStatus,
            values_callable=lambda e: [m.value for m in e],
            native_enum=False,
        ),
        nullable=False,
        default=InboxStatus.NEW,
        index=True,
    )
    classification: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # ``metadata`` is reserved on the declarative base, so the Python attribute
    # is ``metadata_`` while the DB column stays ``metadata``.
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSON, nullable=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Classification retry bookkeeping. The error of the last failed attempt is
    # kept in ``metadata_["error"]``.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Earliest retry time of a failed message; NULL means "now".
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    def __repr__(self) -> str:
        return (
            f"<InboxMessage id={self.id} source={self.source!r} "
            f"external_id={self.external_id!r} status={self.status.value}>"
        )
