"""APScheduler integration.

APScheduler only *triggers* jobs; SQLite (via ``JobRun``) is the source of
truth about actual execution. Each scheduled function is wrapped so that a
``JobRun`` record is created and advanced through its lifecycle independently
of APScheduler's own in-memory state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, job_runs
from app.models.job_run import JobRun

logger = logging.getLogger(__name__)

JobFunc = Callable[[], Awaitable[None]]
JobNotifier = Callable[[AsyncSession, JobRun], Awaitable[None]]

CANCELLED_ERROR = "Job cancelled by application shutdown"


@dataclass(frozen=True)
class JobSpec:
    """A job to register: its name, body, trigger and whether to run at startup."""

    name: str
    func: JobFunc
    trigger: BaseTrigger
    run_at_startup: bool = False
    # False for a frequent, cheap watcher job: it must not write a JobRun on
    # every tick (it triggers real, recorded jobs when there is work to do).
    track_runs: bool = True


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

    A job cancelled during shutdown is recorded as ``failed`` (instead of being
    left ``running``) and is not notified: stopping the service is expected.
    """

    async with db.get_session() as session:
        run = await job_runs.create_job_run(session, job_name=job_name)
        await job_runs.start_job_run(session, run)
        await session.commit()
        run_id = run.id

    error: str | None = None
    cancelled = False
    alert = True
    try:
        await func()
    except asyncio.CancelledError:
        # Only the shutdown path cancels job tasks. Record the outcome and end
        # the task normally so APScheduler does not log it as a crash.
        cancelled = True
        error = CANCELLED_ERROR
        logger.warning("Job %r cancelled by shutdown", job_name)
    except Exception as exc:  # noqa: BLE001 - the scheduler must survive
        error = _format_error(exc)
        # An exception can opt out of the Telegram alert (``alert = False``):
        # the failure is still recorded, e.g. mistakes the user sees in the sheet.
        alert = getattr(exc, "alert", True)
        logger.exception("Job %r failed", job_name)

    async with db.get_session() as session:
        run = await session.get(JobRun, run_id)
        if error is not None:
            await job_runs.fail_job_run(session, run, error=error)
        else:
            await job_runs.succeed_job_run(session, run)
        await session.commit()
        if error is not None and not cancelled and alert and notifier is not None:
            await notifier(session, run)


def wrap_job(
    job_name: str, func: JobFunc, notifier: JobNotifier | None = None
) -> Callable[[], Awaitable[None]]:
    """Return an async function that records and executes ``func`` as a job."""

    async def wrapped() -> None:
        await _execute_job(job_name, func, notifier=notifier)

    return wrapped


def _track(func: JobFunc, running: set[asyncio.Task]) -> JobFunc:
    """Register the job's task in ``running`` while it executes (for shutdown)."""

    async def tracked() -> None:
        task = asyncio.current_task()
        if task is not None:
            running.add(task)
        try:
            await func()
        finally:
            if task is not None:
                running.discard(task)

    return tracked


async def health_check() -> None:
    """Demo job body: a no-op that proves the scheduler pipeline works."""

    logger.info("health_check: ok")


DEFAULT_JOBS = (JobSpec("health_check", health_check, IntervalTrigger(minutes=15, timezone="UTC")),)


def create_scheduler(
    notifier: JobNotifier | None = None,
    *,
    jobs: Sequence[JobSpec] | None = None,
    running: set[asyncio.Task] | None = None,
) -> AsyncIOScheduler:
    """Create the async scheduler and register ``jobs`` (default: ``health_check``).

    Every job runs at most once at a time (``max_instances=1``) and missed runs
    are coalesced into one. When ``running`` is given, the tasks of executing
    jobs are tracked in it so shutdown can wait for them to finish.
    """

    scheduler = AsyncIOScheduler(timezone="UTC")
    for spec in DEFAULT_JOBS if jobs is None else jobs:
        func = (
            wrap_job(spec.name, spec.func, notifier=notifier) if spec.track_runs else spec.func
        )
        if running is not None:
            func = _track(func, running)
        options = {}
        if spec.run_at_startup:
            options["next_run_time"] = datetime.now(timezone.utc)
        scheduler.add_job(
            func,
            trigger=spec.trigger,
            id=spec.name,
            name=spec.name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
            replace_existing=True,
            **options,
        )
    return scheduler
