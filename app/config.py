"""Application configuration loaded from environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class Settings(BaseSettings):
    """Runtime settings.

    Only settings that are actually used at this stage are declared here.
    Future stages will extend this class as new integrations are added.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/personal_manager.sqlite3"
    job_stale_timeout_seconds: int = 3600
    telegram_bot_token: str = ""
    telegram_chat_id: int | None = None
    telegram_poll_timeout: int = 30
    google_service_account_file: str = ""
    google_spreadsheet_id: str = ""
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_seconds: float = 30.0
    gmail_client_secret_file: str = ""
    gmail_token_file: str = ""
    gmail_max_messages: int = 100
    # How often the sheet is checked for edits (one cheap read; a sync runs only
    # after the edits have settled). The full sync still runs every 30 minutes.
    sheets_poll_seconds: int = 5

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {value!r}"
            )
        return level

    @field_validator("job_stale_timeout_seconds")
    @classmethod
    def _validate_stale_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("job_stale_timeout_seconds must be a positive integer")
        return value

    @field_validator("telegram_chat_id", mode="before")
    @classmethod
    def _empty_chat_id_to_none(cls, value):
        if value is None or value == "":
            return None
        return value

    @field_validator("telegram_poll_timeout")
    @classmethod
    def _validate_telegram_poll_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("telegram_poll_timeout must be a positive integer")
        return value

    @field_validator("deepseek_timeout_seconds")
    @classmethod
    def _validate_deepseek_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("deepseek_timeout_seconds must be a positive number")
        return value

    @field_validator("gmail_max_messages")
    @classmethod
    def _validate_gmail_max_messages(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("gmail_max_messages must be a positive integer")
        return value

    @field_validator("sheets_poll_seconds")
    @classmethod
    def _validate_sheets_poll_seconds(cls, value: int) -> int:
        if value < 1:
            raise ValueError("sheets_poll_seconds must be at least 1")
        return value

    @property
    def google_sheets_enabled(self) -> bool:
        """Google Sheets is enabled only when both config values are present."""
        return bool(self.google_service_account_file and self.google_spreadsheet_id)

    @property
    def gmail_enabled(self) -> bool:
        """Gmail is enabled only when both credential paths are configured."""
        return bool(self.gmail_client_secret_file and self.gmail_token_file)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""

    return Settings()
