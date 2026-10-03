"""Tests for Gmail configuration (enabled/disabled behavior)."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.gmail.client import create_client_from_settings


@pytest.fixture(autouse=True)
def _no_gmail_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a real local environment from enabling Gmail (and loading a token)."""

    for name in ("GMAIL_CLIENT_SECRET_FILE", "GMAIL_TOKEN_FILE", "GMAIL_MAX_MESSAGES"):
        monkeypatch.delenv(name, raising=False)


def _settings(**values) -> Settings:
    # ``_env_file=None`` ignores a local ``.env`` that may hold real paths.
    return Settings(_env_file=None, **values)


def test_gmail_disabled_without_any_files() -> None:
    settings = _settings()
    assert settings.gmail_enabled is False
    assert create_client_from_settings(settings) is None


def test_gmail_disabled_without_token_file() -> None:
    settings = _settings(gmail_client_secret_file="client_secret.json")
    assert settings.gmail_enabled is False


def test_gmail_disabled_without_client_secret() -> None:
    settings = _settings(gmail_token_file="token.json")
    assert settings.gmail_enabled is False


def test_gmail_enabled_with_both_files() -> None:
    settings = _settings(
        gmail_client_secret_file="client_secret.json",
        gmail_token_file="token.json",
    )
    assert settings.gmail_enabled is True


def test_create_client_returns_none_when_disabled() -> None:
    assert create_client_from_settings(_settings()) is None


def test_gmail_max_messages_default_and_validation() -> None:
    assert _settings().gmail_max_messages == 100
    with pytest.raises(ValueError):
        _settings(gmail_max_messages=0)
