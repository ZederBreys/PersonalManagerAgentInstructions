"""Smoke tests for the application skeleton."""

from __future__ import annotations

import pytest

from app import __version__
from app.config import Settings, get_settings
from app.main import main


def test_app_package_imports() -> None:
    assert __version__


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.log_level == "INFO"


def test_settings_normalizes_log_level() -> None:
    settings = Settings(log_level="debug")
    assert settings.log_level == "DEBUG"


def test_settings_rejects_unknown_log_level() -> None:
    with pytest.raises(ValueError):
        Settings(log_level="LOUD")


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_main_runs_without_error(schema: None) -> None:
    main()
