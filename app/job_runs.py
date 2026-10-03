"""Job execution state transitions and queries.

The caller owns the transaction: every function here ``flush``es but never
``commit``s, matching the rest of the domain layer. Only the explicitly allowed
status transitions are permitted; everything else raises ``ValueError``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import utcnow
from app.models.job_run import JobRun, JobRunStatus


async def create_job_run(
    session: AsyncSession,
    *,
    job_name: str,
    scheduled_at: datetime | None = None,
) -> JobRun:
    """Create a ``pending`` execution record for ``job_name``."""

    run = JobRun(
        job_name=job_name,
        scheduled_at=scheduled_at or utcnow(),
        status=JobRunStatus.PENDING,
    )
    session.add(run)
    await session.flush()
    return run


async def start_job_run(session: AsyncSession, run: JobRun) -> JobRun:
    """Transition ``pending -> running`` and stamp ``started_at``."""

    if run.status is not JobRunStatus.PENDING:
        raise ValueError(f"Invalid status transition: {run.status.value} -> running")
    run.status = JobRunStatus.RUNNING
    run.started_at = utcnow()
    await session.flush()
    return run


async def succeed_job_run(
    session: AsyncSession, run: JobRun, *, result: str | None = None
) -> JobRun:
    """Transition ``running -> success`` and stamp ``finished_at``.

    Idempotent: calling this on an already-``success`` run is a no-op, so a
    repeated completion never changes a finished job.
    """

    if run.status is JobRunStatus.SUCCESS:
        return run
    if run.status is not JobRunStatus.RUNNING:
        raise ValueError(f"Invalid status transition: {run.status.value} -> success")
    run.status = JobRunStatus.SUCCESS
    run.finished_at = utcnow()
    if result is not None:
        run.result = result
    await session.flush()
    return run


async def fail_job_run(
    session: AsyncSession, run: JobRun, *, error: str | None = None
) -> JobRun:
    """Transition ``running -> failed``, stamp ``finished_at`` and store ``error``.

    Idempotent: calling this on an already-``failed`` run is a no-op.
    """

    if run.status is JobRunStatus.FAILED:
        return run
    if run.status is not JobRunStatus.RUNNING:
        raise ValueError(f"Invalid status transition: {run.status.value} -> failed")
    run.status = JobRunStatus.FAILED
    run.finished_at = utcnow()
    if error is not None:
        run.error = error
    await session.flush()
    return run


async def get_stale_running_jobs(
    session: AsyncSession,
    *,
    timeout: timedelta,
    now: datetime | None = None,
) -> list[JobRun]:
    """Return ``running`` jobs whose ``started_at`` is older than ``timeout``.

    ``now`` defaults to the current time and is expected to be naive UTC (the
    project-wide convention). Detection only; it does *not* change any state.
    """

    now = now or utcnow()
    cutoff = now - timeout
    stmt = (
        select(JobRun)
        .where(JobRun.status == JobRunStatus.RUNNING, JobRun.started_at < cutoff)
        .order_by(JobRun.started_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_latest_job_runs(
    session: AsyncSession,
    *,
    job_name: str,
    limit: int = 10,
) -> list[JobRun]:
    """Return the most recent runs of ``job_name`` (newest first)."""

    stmt = (
        select(JobRun)
        .where(JobRun.job_name == job_name)
        .order_by(JobRun.scheduled_at.desc(), JobRun.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def recover_stale_jobs(
    session: AsyncSession,
    *,
    timeout: timedelta,
    now: datetime | None = None,
    reason: str | None = None,
) -> list[JobRun]:
    """Mark stale ``running`` jobs as ``failed`` (startup recovery).

    This is intentionally simple: it only closes out jobs that were interrupted
    by a process stop. It does not retry them.
    """

    stale = await get_stale_running_jobs(session, timeout=timeout, now=now)
    now = now or utcnow()
    for run in stale:
        run.status = JobRunStatus.FAILED
        run.finished_at = now
        run.error = reason or "Job interrupted by process shutdown"
    if stale:
        await session.flush()
    return stale


async def mark_notification_sent(session: AsyncSession, run: JobRun) -> JobRun:
    """Stamp ``notification_sent_at`` (idempotent, channel-agnostic).

    This layer does not know about Telegram or any other channel: it only
    records that a notification for this run has already been delivered, so a
    caller can use ``notification_sent_at IS NULL`` to decide whether to send.
    """

    if run.notification_sent_at is None:
        run.notification_sent_at = utcnow()
        await session.flush()
    return run


async def prune_finished(
    session: AsyncSession, *, older_than: timedelta, now: datetime | None = None
) -> int:
    """Delete ``success``/``failed`` runs that finished more than ``older_than`` ago.

    ``pending``/``running`` runs are never deleted: they are live state.
    """

    cutoff = (now or utcnow()) - older_than
    result = await session.execute(
        delete(JobRun).where(
            JobRun.status.in_([JobRunStatus.SUCCESS, JobRunStatus.FAILED]),
            JobRun.finished_at < cutoff,
        )
    )
    return result.rowcount or 0
