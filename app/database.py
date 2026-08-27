"""Asynchronous SQLAlchemy engine and session lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db_models import Base


def _prepare_sqlite_directory(database_url: str) -> None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    raw_path = database_url.removeprefix(prefix)
    if raw_path == ":memory:" or raw_path.startswith("file:"):
        return
    Path(raw_path).parent.mkdir(parents=True, exist_ok=True)


class Database:
    def __init__(self, database_url: str) -> None:
        self.url = database_url
        _prepare_sqlite_directory(database_url)
        engine_options: dict[str, object] = {"pool_pre_ping": True}
        if database_url.startswith("sqlite+"):
            engine_options["connect_args"] = {"timeout": 5}

        self.engine: AsyncEngine = create_async_engine(database_url, **engine_options)
        self.sessions = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

        if database_url.startswith("sqlite+"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def configure_sqlite(
                dbapi_connection: Any, _connection_record: Any
            ) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys = ON")
                cursor.execute("PRAGMA journal_mode = WAL")
                cursor.execute("PRAGMA busy_timeout = 5000")
                cursor.close()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def ping(self) -> bool:
        async with self.session() as session:
            result = await session.execute(text("SELECT 1"))
            return result.scalar_one() == 1

    async def schema_ready(self) -> bool:
        required_tables = set(Base.metadata.tables)

        def has_required_tables(connection: Connection) -> bool:
            available_tables = set(inspect(connection).get_table_names())
            return required_tables.issubset(available_tables)

        async with self.engine.connect() as connection:
            return await connection.run_sync(has_required_tables)

    async def dispose(self) -> None:
        await self.engine.dispose()
