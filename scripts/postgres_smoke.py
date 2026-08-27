"""Minimal database integration check used by CI after Alembic migrations."""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.database import Database
from app.models import ServiceCreate
from app.repository import Repository


async def main() -> None:
    settings = Settings.from_env()
    database = Database(settings.database_url)
    repository = Repository(database)
    try:
        if not await database.ping():
            raise RuntimeError("PostgreSQL ping failed")
        created = await repository.create_service(
            ServiceCreate(
                name="CI PostgreSQL probe",
                url="https://ci.example.com/health",
                check_interval_seconds=300,
            ),
            actor="ci",
        )
        loaded = await repository.get_service(created.id)
        if loaded is None or loaded.url != created.url:
            raise RuntimeError("PostgreSQL repository round-trip failed")
        print("PostgreSQL migration and repository round-trip succeeded")
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
