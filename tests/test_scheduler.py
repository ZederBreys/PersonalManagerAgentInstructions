"""Tests for the APScheduler integration and the job wrapper."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import app.db as db
import app.job_runs as job_runs
from app.models.job_run import JobRunStatus
from app.scheduler import _execute_job, create_scheduler, wrap_job


def _run(coro) -> None:
    asyncio.run(coro)


async def _latest_status(name: str) -> JobRunStatus | None:
    async with db.get_session() as s:
        runs = await job_runs.get_latest_job_runs(s, job_name=name, limit=1)
        return runs[0].status if runs else None


def test_wrapper_success_creates_record(schema) -> None:
    async def _r() -> None:
        await _execute_job("ok_job", _ok)
        status = await _latest_status("ok_job")
        assert status is JobRunStatus.SUCCESS

        async with db.get_session() as s:
            run = (await job_runs.get_latest_job_runs(s, job_name="ok_job", limit=1))[0]
            assert run.started_at is not None
            assert run.finished_at is not None
            assert run.started_at <= run.finished_at

    _run(_r())


def test_wrapper_exception_creates_failed(schema) -> None:
    async def _r() -> None:
        await _execute_job("boom_job", _boom)
        status = await _latest_status("boom_job")
        assert status is JobRunStatus.FAILED

        async with db.get_session() as s:
            run = (await job_runs.get_latest_job_runs(s, job_name="boom_job", limit=1))[0]
            assert run.finished_at is not None
            assert "RuntimeError" in run.error
            assert "boom" in run.error

    _run(_r())


def test_wrapper_does_not_re_raise(schema) -> None:
    async def _r() -> None:
        await _execute_job("boom_job", _boom)
        await _execute_job("ok_job", _ok)

        assert await _latest_status("boom_job") is JobRunStatus.FAILED
        assert await _latest_status("ok_job") is JobRunStatus.SUCCESS

    _run(_r())


def test_scheduler_configures_health_check(schema) -> None:
    scheduler = create_scheduler()
    job = scheduler.get_job("health_check")
    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.misfire_grace_time == 60


def test_scheduler_runs_jobs_and_survives_exception(schema) -> None:
    async def _r() -> None:
        scheduler = AsyncIOScheduler(timezone="UTC")
        run_date = datetime.now(timezone.utc) + timedelta(milliseconds=150)
        scheduler.add_job(
            wrap_job("ok_job", _ok),
            trigger=DateTrigger(run_date=run_date),
            id="ok_job",
        )
        scheduler.add_job(
            wrap_job("boom_job", _boom),
            trigger=DateTrigger(run_date=run_date + timedelta(milliseconds=50)),
            id="boom_job",
        )
        scheduler.start()

        deadline = time.monotonic() + 5
        ok_status = boom_status = None
        while time.monotonic() < deadline:
            ok_status = await _latest_status("ok_job")
            boom_status = await _latest_status("boom_job")
            if ok_status is JobRunStatus.SUCCESS and boom_status is JobRunStatus.FAILED:
                break
            await asyncio.sleep(0.05)

        scheduler.shutdown(wait=False)
        await asyncio.sleep(0.05)

        assert ok_status is JobRunStatus.SUCCESS
        assert boom_status is JobRunStatus.FAILED

    _run(_r())


async def _ok() -> None:
    return None


async def _boom() -> None:
    raise RuntimeError("boom")
