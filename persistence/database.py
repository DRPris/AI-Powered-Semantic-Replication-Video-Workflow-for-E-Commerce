"""Async SQLAlchemy engine and session lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
import os

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://video:video@postgres:5432/video_replication"
)


def _configured_database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def get_engine(database_url: str | None = None) -> AsyncEngine:
    global _engine
    if database_url is not None:
        return create_async_engine(database_url, pool_pre_ping=True)
    if _engine is None:
        _engine = create_async_engine(
            _configured_database_url(),
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
        )
    return _engine


def get_session_factory(
    database_url: str | None = None,
) -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if database_url is not None:
        return async_sessionmaker(
            get_engine(database_url),
            expire_on_commit=False,
        )
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
        )
    return _session_factory


async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def close_database() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
