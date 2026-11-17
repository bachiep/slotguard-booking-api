"""Async SQLAlchemy 2.0 database engine, session management, and UTC column adapter."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy import DateTime, MetaData, TypeDecorator, event
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from slotguard.core.clock import ensure_utc

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative ORM base with consistent constraint naming."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UTCDateTime(TypeDecorator[datetime]):
    """Store datetimes as timezone-aware UTC across both PostgreSQL and SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value)


def is_sqlite_url(database_url: str) -> bool:
    """Return True when the SQLAlchemy URL targets SQLite."""
    return database_url.startswith("sqlite")


def create_engine_and_sessionmaker(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Build an async SQLAlchemy engine and session factory."""
    connect_args: dict[str, Any] = {}
    if is_sqlite_url(database_url):
        connect_args["timeout"] = 30.0

    engine = create_async_engine(
        database_url,
        echo=False,
        pool_pre_ping=not is_sqlite_url(database_url),
        connect_args=connect_args,
    )

    if is_sqlite_url(database_url):

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite_pragmas(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.close()

    session_maker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return engine, session_maker


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a scoped AsyncSession per request."""
    session_maker: async_sessionmaker[AsyncSession] = request.app.state.session_maker
    async with session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
