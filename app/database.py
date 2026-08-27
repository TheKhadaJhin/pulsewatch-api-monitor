"""SQLite connection management and schema creation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    method TEXT NOT NULL CHECK (method IN ('GET', 'HEAD')),
    expected_status INTEGER NOT NULL CHECK (expected_status BETWEEN 100 AND 599),
    timeout_seconds REAL NOT NULL CHECK (timeout_seconds > 0),
    retry_count INTEGER NOT NULL DEFAULT 2 CHECK (retry_count BETWEEN 0 AND 3),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    status_code INTEGER,
    latency_ms REAL,
    is_healthy INTEGER NOT NULL CHECK (is_healthy IN (0, 1)),
    attempts INTEGER NOT NULL CHECK (attempts >= 1),
    error_type TEXT,
    error_message TEXT,
    FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_check_results_service_time
ON check_results(service_id, checked_at DESC);

CREATE INDEX IF NOT EXISTS idx_check_results_checked_at
ON check_results(checked_at DESC);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as connection:
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.executescript(SCHEMA)
            await connection.commit()

    async def ping(self) -> bool:
        async with self.connect() as connection:
            cursor = await connection.execute("SELECT 1")
            row = await cursor.fetchone()
        return bool(row and row[0] == 1)
