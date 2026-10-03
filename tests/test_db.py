"""Infrastructure tests for the database layer (no domain models yet)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import app.db as db
from app.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'test.sqlite3').as_posix()}"


def _alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_engine_connects_and_executes_query(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)

    async def _select_one() -> int:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT 1"))
                return result.scalar_one()
        finally:
            await engine.dispose()

    assert asyncio.run(_select_one()) == 1


def test_lazy_engine_and_session_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        db, "get_settings", lambda: Settings(database_url=_sqlite_url(tmp_path))
    )
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)

    async def _use_session() -> int:
        async with db.get_session() as session:
            result = await session.execute(text("SELECT 2"))
            return result.scalar_one()

    try:
        assert db._engine is None  # lazy: nothing created on import
        assert asyncio.run(_use_session()) == 2
        assert db._engine is not None  # created on demand
    finally:
        asyncio.run(db.dispose_engine())

    assert db._engine is None
    assert db._session_factory is None


def test_alembic_upgrade_head_is_idempotent(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    config = _alembic_config(url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")  # must be a safe no-op

    async def _read_versions() -> list[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT version_num FROM alembic_version")
                )
                return [row[0] for row in result]
        finally:
            await engine.dispose()

    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    assert asyncio.run(_read_versions()) == [head]


def _db_file(url: str) -> str:
    return make_url(url).database or ""


def _list_tables(url: str) -> set[str]:
    connection = sqlite3.connect(_db_file(url))
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()


def test_migration_lifecycle_creates_and_drops_schema(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    config = _alembic_config(url)
    domain_tables = {"events", "reminders", "recurring_expenses"}

    command.upgrade(config, "head")
    assert domain_tables <= _list_tables(url)

    command.downgrade(config, "base")
    assert domain_tables & _list_tables(url) == set()

    command.upgrade(config, "head")
    assert domain_tables <= _list_tables(url)
