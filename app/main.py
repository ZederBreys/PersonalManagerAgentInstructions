"""Application entry point: one long-running asyncio process for systemd.

Lifecycle::

    load config (invalid -> exit 2) -> logging -> signal handlers
    -> build clients (no network, no OAuth browser flow)
    -> startup recovery of interrupted jobs (DB unusable -> exit 1)
    -> register jobs, start scheduler -> start Telegram polling
    -> wait for SIGTERM/SIGINT
    -> stop polling -> pause scheduler, let running jobs finish (bounded)
    -> shut scheduler down -> close clients -> dispose DB engine -> exit 0

Recoverable errors (a failing job, Telegram/Gmail/Sheets/DeepSeek outages)
never stop the process: jobs record them in ``JobRun`` and alert via Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app import db
from app.config import Settings, get_settings
from app.deepseek.client import DeepSeekClient
from app.google_sheets.client import create_client_from_settings as create_sheets_client
from app.jobs import Services, build_jobs, recover_stale_jobs_and_notify
from app.logging_setup import setup_logging
from app.notifications import make_streak_failure_notifier
from app.scheduler import create_scheduler
from app.telegram.bot import run_polling
from app.telegram.client import TelegramClient

logger = logging.getLogger(__name__)

# Upper bound for running jobs to finish after SIGTERM (systemd's default
# TimeoutStopSec is 90s); unfinished jobs are cancelled and recorded as failed.
JOB_SHUTDOWN_GRACE_SECONDS = 30.0

_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class StartupError(RuntimeError):
    """The application cannot start in a working state."""


async def run_startup_recovery(
    timeout_seconds: int,
    *,
    telegram: TelegramClient | None = None,
    chat_id: int | None = None,
) -> int:
    """Mark stale ``running`` jobs as failed; return how many were recovered.

    Runs before the scheduler starts so jobs interrupted by a process stop do
    not linger in the ``running`` state across restarts. After recovery is
    committed, each recovered job is notified via Telegram (if configured).
    """

    return await recover_stale_jobs_and_notify(
        timeout_seconds, telegram=telegram, chat_id=chat_id
    )


def build_services(settings: Settings) -> Services:
    """Create the long-lived clients. Nothing here performs network I/O."""

    telegram = (
        TelegramClient(settings.telegram_bot_token, poll_timeout=settings.telegram_poll_timeout)
        if settings.telegram_bot_token
        else None
    )
    deepseek = (
        DeepSeekClient(
            settings.deepseek_api_key,
            model=settings.deepseek_model,
            http_timeout=settings.deepseek_timeout_seconds,
        )
        if settings.deepseek_api_key
        else None
    )
    return Services(
        settings=settings,
        telegram=telegram,
        sheets=create_sheets_client(settings),
        deepseek=deepseek,
    )


def _install_signal_handlers(stop: asyncio.Event) -> dict:
    """Route SIGTERM/SIGINT to ``stop``; return what is needed to undo it."""

    loop = asyncio.get_running_loop()
    previous: dict = {}
    for sig in _STOP_SIGNALS:
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loop (development only)
            previous[sig] = signal.signal(
                sig, lambda *_: loop.call_soon_threadsafe(stop.set)
            )
    return previous


def _remove_signal_handlers(previous: dict) -> None:
    loop = asyncio.get_running_loop()
    for sig in _STOP_SIGNALS:
        if sig in previous:
            signal.signal(sig, previous[sig])
        else:
            loop.remove_signal_handler(sig)


def _log_polling_exit(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Telegram polling crashed", exc_info=exc)


async def _stop_scheduler(scheduler, running_jobs: set[asyncio.Task]) -> None:
    """Stop triggering jobs, give running ones time to finish, then shut down."""

    scheduler.pause()
    if running_jobs:
        logger.info("Waiting for %d running job(s) to finish", len(running_jobs))
        _, pending = await asyncio.wait(
            set(running_jobs), timeout=JOB_SHUTDOWN_GRACE_SECONDS
        )
        for task in pending:
            task.cancel()
        if pending:
            # Cancelled jobs record themselves as failed before finishing.
            await asyncio.gather(*pending, return_exceptions=True)
    scheduler.shutdown(wait=False)


async def run(
    settings: Settings,
    *,
    stop: asyncio.Event | None = None,
    services: Services | None = None,
) -> None:
    """Run the application until ``stop`` is set (by SIGTERM/SIGINT by default)."""

    stop = stop or asyncio.Event()
    previous_handlers = _install_signal_handlers(stop)
    services = services or build_services(settings)
    scheduler = None
    running_jobs: set[asyncio.Task] = set()
    poller: asyncio.Task | None = None
    try:
        try:
            recovered = await run_startup_recovery(
                settings.job_stale_timeout_seconds,
                telegram=services.telegram,
                chat_id=services.chat_id,
            )
        except SQLAlchemyError as exc:
            raise StartupError(
                "database is unavailable or not migrated (run `alembic upgrade head`): "
                f"{type(exc).__name__}"
            ) from exc
        if recovered:
            logger.warning("Recovered %d stale job run(s) as failed", recovered)

        jobs = build_jobs(services)
        scheduler = create_scheduler(
            make_streak_failure_notifier(services.telegram, services.chat_id),
            jobs=jobs,
            running=running_jobs,
        )
        scheduler.start()
        for job in scheduler.get_jobs():
            logger.info("Scheduled job %s (next run %s)", job.id, job.next_run_time)

        if services.telegram is not None and services.chat_id is not None:
            poller = asyncio.create_task(
                run_polling(
                    services.telegram,
                    chat_id=services.chat_id,
                    job_names=[job.name for job in jobs],
                ),
                name="telegram-polling",
            )
            poller.add_done_callback(_log_polling_exit)
        else:
            logger.info("Telegram polling disabled (TELEGRAM_BOT_TOKEN/CHAT_ID not set)")

        logger.info("Application started")
        await stop.wait()
        logger.info("Shutdown requested")
    finally:
        await _shutdown(services, scheduler, running_jobs, poller)
        _remove_signal_handlers(previous_handlers)


async def _shutdown(services, scheduler, running_jobs, poller) -> None:
    """Release everything in dependency order; each step runs even if one fails."""

    if poller is not None:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
    if scheduler is not None and scheduler.running:
        try:
            await _stop_scheduler(scheduler, running_jobs)
        except Exception:  # noqa: BLE001 - keep closing the remaining resources
            logger.exception("Scheduler shutdown failed")
    # Jobs may use the clients (e.g. failure alerts), so close them afterwards.
    await services.aclose()
    try:
        await db.dispose_engine()
    except Exception:  # noqa: BLE001
        logger.exception("Database engine dispose failed")


def main() -> None:
    """Run the application until SIGTERM/SIGINT; exit non-zero if it cannot start."""

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Invalid configuration:\n{exc}", file=sys.stderr)
        sys.exit(2)
    setup_logging(settings.log_level)

    logger.info("Personal Manager v%s starting", __version__)
    logger.info("Configuration loaded (log_level=%s)", settings.log_level)

    try:
        asyncio.run(run(settings))
    except StartupError as exc:
        logger.critical("Startup failed: %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:  # Ctrl+C before the signal handlers were installed
        pass
    except Exception:
        logger.critical("Application crashed", exc_info=True)
        sys.exit(1)
    logger.info("Application stopped")


if __name__ == "__main__":
    main()
