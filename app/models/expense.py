"""Recurring expense domain model."""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class ExpensePeriod(str, enum.Enum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class RecurringExpense(Base):
    __tablename__ = "recurring_expenses"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_recurring_expenses_amount_positive"),
        CheckConstraint(
            "payment_day BETWEEN 1 AND 31", name="ck_recurring_expenses_payment_day"
        ),
        CheckConstraint("currency <> ''", name="ck_recurring_expenses_currency_nonempty"),
        CheckConstraint(
            "period IN ('monthly', 'quarterly', 'yearly')",
            name="ck_recurring_expenses_period",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Money is stored as integer minor units (e.g. 500 = 5.00 EUR) to avoid
    # floating point errors in SQLite. See the report for the reasoning.
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    period: Mapped[ExpensePeriod] = mapped_column(
        Enum(
            ExpensePeriod,
            values_callable=lambda e: [m.value for m in e],
            native_enum=False,
        ),
        nullable=False,
        default=ExpensePeriod.MONTHLY,
    )
    payment_day: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    next_payment_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # One-time key of the Google Sheets row this expense was created from;
    # makes creation from Sheets idempotent (see app.google_sheets.sync).
    sheet_key: Mapped[str | None] = mapped_column(
        String(32), nullable=True, unique=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<RecurringExpense id={self.id} name={self.name!r} "
            f"amount_minor={self.amount_minor} {self.currency}>"
        )
