"""Job execution tracking model.

``JobRun`` is the persisted record of a scheduled job execution. The database
(not APScheduler) is the source of truth for whether a job was scheduled,
started, finished successfully, failed, or was left stuck in ``running`` after
a process crash.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class JobRunStatus(str, enum.Enum):
    """Allowed lifecycle states of a job run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        # SQLite Enum has no native type and does not auto-emit a CHECK, so
        # enforce the allowed status values explicitly.
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed')",
            name="ck_job_runs_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[JobRunStatus] = mapped_column(
        Enum(
            JobRunStatus,
            values_callable=lambda e: [m.value for m in e],
            native_enum=False,
        ),
        nullable=False,
        default=JobRunStatus.PENDING,
        index=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    notification_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    def __repr__(self) -> str:
        return f"<JobRun id={self.id} job_name={self.job_name!r} status={self.status.value}>"
