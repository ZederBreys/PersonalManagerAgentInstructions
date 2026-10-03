"""Database infrastructure: async engine, session factory and declarative base.

The engine and session factory are created lazily on first use, so importing
application modules has no side effects and no connections are opened until the
database is actually needed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all future ORM models."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def utcnow() -> datetime:
    """Return the current naive UTC datetime, used for timestamp defaults."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _set_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    """Enable SQLite foreign-key enforcement for a new DBAPI connection.

    SQLite only enforces ``FOREIGN KEY`` constraints when ``PRAGMA
    foreign_keys`` is enabled, and the setting is per-connection. Attaching this
    as a ``connect`` listener ensures it is on for every connection we create.
    """

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def ensure_sqlite_parent_dir(url: str) -> None:
    """Create the parent directory of a file-based SQLite database if needed.

    SQLAlchemy does not create missing directories for SQLite files, so this
    keeps a relative default like ``./data/...`` working out of the box.
    """

    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return

    database = parsed.database
    if not database or database == ":memory:":
        return

    Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""

    global _engine
    if _engine is None:
        url = get_settings().database_url
        ensure_sqlite_parent_dir(url)
        _engine = create_async_engine(url)
        if _engine.dialect.name == "sqlite":
            event.listen(_engine.sync_engine, "connect", _set_sqlite_foreign_keys)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory."""

    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session and close it when the context block exits."""

    async with get_session_factory()() as session:
        yield session


async def dispose_engine() -> None:
    """Dispose the engine and reset lazy singletons (shutdown/tests)."""

    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
