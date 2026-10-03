"""Telegram Bot API transport client (read-only + sendMessage only)."""

from app.telegram.client import TelegramAPIError, TelegramClient

__all__ = ["TelegramAPIError", "TelegramClient"]
