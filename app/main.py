"""Application entry point.

Loads configuration, initialises logging, performs startup recovery of
interrupted jobs (notifying via Telegram if configured), then reports that it
started and stopped. External integrations (scheduler, Gmail, Google Sheets,
AI) are intentionally not wired up yet.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from app import __version__
from app import db
from app.config import Settings, get_settings
from app.job_runs import recover_stale_jobs
from app.logging_setup import setup_logging
from app.notifications import notify_job_interrupted
from app.telegram.client import TelegramClient

logger = logging.getLogger(__name__)


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

    timeout = timedelta(seconds=timeout_seconds)
    async with db.get_session() as session:
        recovered = await recover_stale_jobs(session, timeout=timeout)
        await session.commit()
        if telegram is not None and chat_id is not None:
            for run in recovered:
                await notify_job_interrupted(
                    session, run, telegram=telegram, chat_id=chat_id
                )
    return len(recovered)


async def _recover_with_notifications(settings: Settings) -> int:
    telegram: TelegramClient | None = None
    if settings.telegram_bot_token and settings.telegram_chat_id is not None:
        telegram = TelegramClient(settings.telegram_bot_token)
    try:
        return await run_startup_recovery(
            settings.job_stale_timeout_seconds,
            telegram=telegram,
            chat_id=settings.telegram_chat_id,
        )
    finally:
        if telegram is not None:
            await telegram.aclose()


def main() -> None:
    """Run the application skeleton once and exit cleanly."""

    settings = get_settings()
    setup_logging(settings.log_level)

    logger.info("Personal Manager v%s starting", __version__)
    logger.info("Configuration loaded (log_level=%s)", settings.log_level)

    recovered = asyncio.run(_recover_with_notifications(settings))
    if recovered:
        logger.warning("Recovered %d stale job run(s) as failed", recovered)

    logger.info("Application started")
    logger.info("Application stopped")


if __name__ == "__main__":
    main()
