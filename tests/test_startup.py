"""Startup recovery integration tests."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app import db
from app.db import utcnow
from app.job_runs import create_job_run, start_job_run
from app.main import run_startup_recovery
from app.models.job_run import JobRun, JobRunStatus


def test_startup_recovery_marks_stale_running_job_failed(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            run = await create_job_run(session, job_name="health_check")
            await start_job_run(session, run)
            run.started_at = utcnow() - timedelta(days=2)
            await session.commit()
            run_id = run.id

        recovered = await run_startup_recovery(3600)
        assert recovered == 1

        async with db.get_session() as session:
            loaded = await session.get(JobRun, run_id)
            assert loaded is not None
            assert loaded.status is JobRunStatus.FAILED
            assert loaded.finished_at is not None
            assert loaded.error

    asyncio.run(_run())


def test_startup_recovery_leaves_fresh_running_job_alone(schema: None) -> None:
    async def _run() -> None:
        async with db.get_session() as session:
            run = await create_job_run(session, job_name="health_check")
            await start_job_run(session, run)
            await session.commit()
            run_id = run.id

        recovered = await run_startup_recovery(3600)
        assert recovered == 0

        async with db.get_session() as session:
            loaded = await session.get(JobRun, run_id)
            assert loaded is not None
            assert loaded.status is JobRunStatus.RUNNING
            assert loaded.finished_at is None

    asyncio.run(_run())
