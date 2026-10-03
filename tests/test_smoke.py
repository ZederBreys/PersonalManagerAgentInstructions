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


def test_main_starts_and_stops_cleanly(schema: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` blocks while running; here it is stopped shortly after startup."""

    import asyncio

    import app.main as main_module

    real_run = main_module.run

    async def run_then_stop(settings: Settings) -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.2, stop.set)
        await real_run(settings, stop=stop)

    monkeypatch.setattr(main_module, "run", run_then_stop)
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(_env_file=None))
    main()
