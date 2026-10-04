"""Email addresses whose messages the Gmail import may read (the "Email" sheet)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class AllowedSender(Base):
    __tablename__ = "allowed_senders"
    __table_args__ = (
        CheckConstraint("email <> ''", name="ck_allowed_senders_email_nonempty"),
        CheckConstraint("email = lower(email)", name="ck_allowed_senders_email_lowercase"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Normalized (trimmed, lower case); compared exactly, never by domain or substring.
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # One-time key of the sheet row the record was created from (idempotent create).
    sheet_key: Mapped[str | None] = mapped_column(
        String(32), nullable=True, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    def __repr__(self) -> str:
        return f"<AllowedSender id={self.id} email={self.email!r} active={self.is_active}>"
