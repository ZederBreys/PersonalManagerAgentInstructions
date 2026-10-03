"""Shared fixtures for domain tests (temp SQLite schema via the app engine)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import app.db as db
from app.config import Settings
from app.db import Base
from app.models import Event, RecurringExpense, Reminder  # noqa: F401


@pytest.fixture
def schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create a full schema on a temp SQLite DB (FK pragma on) per test."""

    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.sqlite3').as_posix()}"
    monkeypatch.setattr(db, "get_settings", lambda: Settings(database_url=url))
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)

    async def _create_schema() -> None:
        engine = db.get_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(_create_schema())
    yield
    asyncio.run(db.dispose_engine())
