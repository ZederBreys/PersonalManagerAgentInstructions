"""Telegram long polling and read-only command handling.

Runs inside the application's asyncio process. Only messages from the
configured ``TELEGRAM_CHAT_ID`` are answered; everything else is ignored.
Commands are read-only: nothing here mutates application state.

Natural-language queries ("сколько я трачу на подписки?") are not implemented
yet — no query logic exists in the project — so free text gets a short
explanation instead of a guessed answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from app import db, job_runs
from app.telegram.client import TelegramAPIError, TelegramClient

logger = logging.getLogger(__name__)

RETRY_INITIAL_SECONDS = 1.0
RETRY_MAX_SECONDS = 60.0
# Errors that retrying cannot fix: the token is invalid or revoked.
_FATAL_ERROR_CODES = {401, 404}

HELP_TEXT = (
    "Личный менеджер.\n\n"
    "Команды:\n"
    "/status — состояние фоновых задач\n"
    "/help — эта справка"
)
UNSUPPORTED_TEXT = (
    "Вопросы в свободной форме пока не поддерживаются.\n\n" + HELP_TEXT
)


def _format_time(value) -> str:
    return value.strftime("%d.%m %H:%M") + " UTC" if value else "-"


async def status_text(job_names: Sequence[str]) -> str:
    """Describe the latest run of every scheduled job (read-only)."""

    lines = ["Фоновые задачи:"]
    async with db.get_session() as session:
        for name in job_names:
            runs = await job_runs.get_latest_job_runs(session, job_name=name, limit=1)
            if not runs:
                lines.append(f"• {name}: ещё не запускалась")
                continue
            run = runs[0]
            line = f"• {name}: {run.status.value}, {_format_time(run.started_at)}"
            if run.error and run.status.value == "failed":
                line += f"\n  {run.error[:200]}"
            lines.append(line)
    return "\n".join(lines)


async def answer(text: str | None, job_names: Sequence[str]) -> str:
    """Return the reply for an incoming message text."""

    words = (text or "").split()
    # "/status@my_bot" (group-style mention) is the same command as "/status".
    command = words[0].split("@")[0].lower() if words else ""
    if command in ("/start", "/help"):
        return HELP_TEXT
    if command == "/status":
        return await status_text(job_names)
    return UNSUPPORTED_TEXT


def _message_chat_and_text(update: dict[str, Any]) -> tuple[int | None, str | None]:
    message = update.get("message")
    if not isinstance(message, dict):
        return None, None
    chat = message.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    text = message.get("text")
    return (chat_id if isinstance(chat_id, int) else None), (
        text if isinstance(text, str) else None
    )


async def handle_update(
    telegram: TelegramClient,
    update: dict[str, Any],
    *,
    chat_id: int,
    job_names: Sequence[str],
) -> None:
    """Answer one update if it is a message from the owner's chat."""

    sender_chat, text = _message_chat_and_text(update)
    if sender_chat is None:
        return
    if sender_chat != chat_id:
        logger.info("Ignoring Telegram message from a foreign chat")
        return
    await telegram.send_message(chat_id=chat_id, text=await answer(text, job_names))


async def run_polling(
    telegram: TelegramClient,
    *,
    chat_id: int,
    job_names: Sequence[str],
    retry_initial: float = RETRY_INITIAL_SECONDS,
    retry_max: float = RETRY_MAX_SECONDS,
) -> None:
    """Long-poll ``getUpdates`` until cancelled.

    Transient API/network errors are retried with exponential backoff (capped
    at ``retry_max``). An invalid token stops polling with an error log; the
    rest of the application keeps running. A failure while handling one update
    is logged and the update is acknowledged, so it is never retried forever.
    """

    offset: int | None = None
    delay = retry_initial
    logger.info("Telegram polling started")
    while True:
        try:
            updates = await telegram.get_updates(offset=offset)
        except TelegramAPIError as exc:
            if exc.error_code in _FATAL_ERROR_CODES or exc.http_status in _FATAL_ERROR_CODES:
                logger.error("Telegram polling stopped: %s (check TELEGRAM_BOT_TOKEN)", exc)
                return
            logger.warning("Telegram getUpdates failed: %s; retrying in %.0fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, retry_max)
            continue
        delay = retry_initial

        if not isinstance(updates, list):
            logger.warning("Malformed Telegram getUpdates result; ignoring")
            continue
        for update in updates:
            if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                continue
            offset = update["update_id"] + 1
            try:
                await handle_update(telegram, update, chat_id=chat_id, job_names=job_names)
            except TelegramAPIError as exc:
                logger.warning("Telegram reply not sent: %s", exc)
            except Exception:  # noqa: BLE001 - one bad update must not stop polling
                logger.exception("Failed to handle Telegram update %s", update["update_id"])
