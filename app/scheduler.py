"""APScheduler integration.

APScheduler only *triggers* jobs; SQLite (via ``JobRun``) is the source of
truth about actual execution. Each scheduled function is wrapped so that a
``JobRun`` record is created and advanced through its lifecycle independently
of APScheduler's own in-memory state.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, job_runs
from app.models.job_run import JobRun

logger = logging.getLogger(__name__)

JobFunc = Callable[[], Awaitable[None]]
JobNotifier = Callable[[AsyncSession, JobRun], Awaitable[None]]


def _format_error(exc: BaseException) -> str:
    """Return a concise textual representation of an exception."""

    return f"{type(exc).__name__}: {exc}"


async def _execute_job(
    job_name: str, func: JobFunc, notifier: JobNotifier | None = None
) -> None:
    """Run ``func`` under a ``JobRun`` record (pending -> running -> terminal).

    The record is committed in two phases so that a process crash *during* the
    job body still leaves a durable ``running`` record (with ``started_at``)
    that startup recovery can later detect and close. The job body's exception
    is recorded and logged but never re-raised, so a single failing job cannot
    crash the scheduler. On failure, an optional ``notifier`` is invoked after
    the ``failed`` state is committed.
    """

    async with db.get_session() as session:
        run = await job_runs.create_job_run(session, job_name=job_name)
        await job_runs.start_job_run(session, run)
        await session.commit()
        run_id = run.id

    error: str | None = None
    try:
        await func()
    except Exception as exc:  # noqa: BLE001 - the scheduler must survive
        error = _format_error(exc)
        logger.exception("Job %r failed", job_name)

    async with db.get_session() as session:
        run = await session.get(JobRun, run_id)
        if error is not None:
            await job_runs.fail_job_run(session, run, error=error)
        else:
            await job_runs.succeed_job_run(session, run)
        await session.commit()
        if error is not None and notifier is not None:
            await notifier(session, run)


def wrap_job(
    job_name: str, func: JobFunc, notifier: JobNotifier | None = None
) -> Callable[[], Awaitable[None]]:
    """Return an async function that records and executes ``func`` as a job."""

    async def wrapped() -> None:
        await _execute_job(job_name, func, notifier=notifier)

    return wrapped


async def health_check() -> None:
    """Demo job body: a no-op that proves the scheduler pipeline works."""

    logger.info("health_check: ok")


def create_scheduler(notifier: JobNotifier | None = None) -> AsyncIOScheduler:
    """Create and configure the async scheduler with the demo ``health_check`` job."""

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        wrap_job("health_check", health_check, notifier=notifier),
        trigger=IntervalTrigger(minutes=15),
        id="health_check",
        name="health_check",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        replace_existing=True,
    )
    return scheduler
