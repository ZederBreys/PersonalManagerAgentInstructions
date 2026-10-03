"""Tests for job execution tracking (transitions, stale detection, recovery)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.db as db
import app.job_runs as job_runs
from app.models.job_run import JobRun, JobRunStatus

NOW = datetime(2026, 9, 27, 10, 0, 0)


def _run(coro) -> None:
    asyncio.run(coro)


async def _make_running(session, name: str, started_at: datetime | None = None):
    run = await job_runs.create_job_run(session, job_name=name, scheduled_at=NOW)
    await job_runs.start_job_run(session, run)
    if started_at is not None:
        run.started_at = started_at
        await session.flush()
    return run


def test_create_job_run_is_pending(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            run = await job_runs.create_job_run(s, job_name="health_check", scheduled_at=NOW)
            await s.commit()
            rid = run.id
        async with db.get_session() as s:
            loaded = await s.get(JobRun, rid)
            assert loaded.status is JobRunStatus.PENDING
            assert loaded.scheduled_at == NOW
            assert loaded.started_at is None
            assert loaded.finished_at is None
            assert loaded.error is None
            assert loaded.result is None

    _run(_r())


def test_pending_to_running(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            run = await job_runs.create_job_run(s, job_name="health_check")
            await job_runs.start_job_run(s, run)
            await s.commit()
            assert run.status is JobRunStatus.RUNNING
            assert run.started_at is not None
            assert run.finished_at is None

    _run(_r())


def test_running_to_success(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            run = await _make_running(s, "health_check")
            await job_runs.succeed_job_run(s, run, result="ok")
            await s.commit()
            assert run.status is JobRunStatus.SUCCESS
            assert run.finished_at is not None
            assert run.result == "ok"

    _run(_r())


def test_running_to_failed(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            run = await _make_running(s, "health_check")
            await job_runs.fail_job_run(s, run, error="boom")
            await s.commit()
            assert run.status is JobRunStatus.FAILED
            assert run.finished_at is not None
            assert run.error == "boom"

    _run(_r())


def test_invalid_transitions_rejected(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            pending = await job_runs.create_job_run(s, job_name="x")
            with pytest.raises(ValueError):
                await job_runs.succeed_job_run(s, pending)  # pending -> success
            with pytest.raises(ValueError):
                await job_runs.fail_job_run(s, pending, error="x")  # pending -> failed

            running = await _make_running(s, "x")
            with pytest.raises(ValueError):
                await job_runs.start_job_run(s, running)  # running -> running

            success = await job_runs.create_job_run(s, job_name="y")
            await job_runs.start_job_run(s, success)
            await job_runs.succeed_job_run(s, success)
            with pytest.raises(ValueError):
                await job_runs.fail_job_run(s, success, error="x")  # success -> failed
            with pytest.raises(ValueError):
                await job_runs.start_job_run(s, success)  # success -> running

    _run(_r())


def test_repeat_completion_is_idempotent(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            run = await _make_running(s, "health_check")
            await job_runs.succeed_job_run(s, run)
            finished = run.finished_at
            await job_runs.succeed_job_run(s, run)  # second call is a no-op
            assert run.status is JobRunStatus.SUCCESS
            assert run.finished_at == finished

            failed = await _make_running(s, "other")
            await job_runs.fail_job_run(s, failed, error="boom")
            failed_finished = failed.finished_at
            await job_runs.fail_job_run(s, failed, error="other")  # no-op
            assert failed.status is JobRunStatus.FAILED
            assert failed.error == "boom"
            assert failed.finished_at == failed_finished

    _run(_r())


def test_get_stale_running_jobs(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            stale = await _make_running(
                s, "a", started_at=NOW - timedelta(hours=1)
            )
            fresh = await _make_running(
                s, "b", started_at=NOW - timedelta(minutes=5)
            )
            await s.commit()

            found = await job_runs.get_stale_running_jobs(
                s, timeout=timedelta(minutes=30), now=NOW
            )
            assert {r.id for r in found} == {stale.id}

            # A short timeout makes both runs stale.
            both = await job_runs.get_stale_running_jobs(
                s, timeout=timedelta(minutes=1), now=NOW
            )
            assert {r.id for r in both} == {stale.id, fresh.id}

            # Relative to an earlier 'now', nothing is stale yet.
            none_found = await job_runs.get_stale_running_jobs(
                s, timeout=timedelta(minutes=30), now=NOW - timedelta(minutes=40)
            )
            assert none_found == []

    _run(_r())


def test_recover_stale_jobs_marks_failed(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            stale = await _make_running(
                s, "health_check", started_at=NOW - timedelta(hours=2)
            )
            await s.commit()
            sid = stale.id

            recovered = await job_runs.recover_stale_jobs(
                s, timeout=timedelta(minutes=30), now=NOW
            )
            await s.commit()
            assert [r.id for r in recovered] == [sid]

        async with db.get_session() as s:
            loaded = await s.get(JobRun, sid)
            assert loaded.status is JobRunStatus.FAILED
            assert loaded.finished_at == NOW
            assert loaded.error is not None

    _run(_r())


def test_get_latest_job_runs_ordered(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            for i, dt in enumerate(
                [NOW - timedelta(days=2), NOW - timedelta(days=1), NOW]
            ):
                run = await job_runs.create_job_run(
                    s, job_name="health_check", scheduled_at=dt
                )
                await job_runs.start_job_run(s, run)
                await job_runs.succeed_job_run(s, run)
            await s.commit()

            runs = await job_runs.get_latest_job_runs(
                s, job_name="health_check", limit=2
            )
            assert [r.scheduled_at for r in runs] == [
                NOW,
                NOW - timedelta(days=1),
            ]

    _run(_r())


def test_invalid_status_rejected_at_db_level(schema) -> None:
    async def _r() -> None:
        async with db.get_session() as s:
            with pytest.raises(IntegrityError):
                await s.execute(
                    text(
                        "INSERT INTO job_runs "
                        "(job_name, scheduled_at, status, created_at) "
                        "VALUES ('x', '2026-01-01 00:00:00', 'weird', "
                        "'2026-01-01 00:00:00')"
                    )
                )

    _run(_r())
