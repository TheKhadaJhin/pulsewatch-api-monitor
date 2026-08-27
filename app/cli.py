"""Command-line utilities for initializing and demonstrating PulseWatch."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3

from app.config import Settings
from app.database import Database
from app.models import ServiceCreate
from app.monitoring import ApiMonitor
from app.repository import Repository

DEMO_SERVICES = (
    ServiceCreate(
        name="GitHub API",
        url="https://api.github.com",
        expected_status=200,
        timeout_seconds=5,
        retry_count=2,
    ),
    ServiceCreate(
        name="JSONPlaceholder",
        url="https://jsonplaceholder.typicode.com/posts/1",
        expected_status=200,
        timeout_seconds=5,
        retry_count=2,
    ),
)


async def build_components() -> tuple[Database, Repository, ApiMonitor]:
    settings = Settings.from_env()
    database = Database(settings.database_path)
    await database.initialize()
    repository = Repository(database)
    return database, repository, ApiMonitor(repository, settings)


async def init_database() -> None:
    database, _, _ = await build_components()
    print(f"Database ready: {database.path}")


async def seed_database() -> None:
    _, repository, _ = await build_components()
    created = 0
    for service in DEMO_SERVICES:
        try:
            await repository.create_service(service)
            created += 1
            print(f"Added: {service.name}")
        except sqlite3.IntegrityError:
            print(f"Already exists: {service.name}")
    print(f"Seed complete: {created} service(s) added")


async def run_all_checks() -> None:
    _, _, monitor = await build_components()
    results = await monitor.run_checks()
    if not results:
        print("No active services. Run `python -m app.cli seed` first.")
        return
    for result in results:
        marker = "OK" if result.is_healthy else "FAIL"
        latency = f"{result.latency_ms:.2f} ms" if result.latency_ms else "n/a"
        print(
            f"[{marker}] service={result.service_id} "
            f"status={result.status_code} latency={latency} "
            f"attempts={result.attempts}"
        )


async def print_summary(window_hours: int) -> None:
    _, repository, _ = await build_components()
    settings = Settings.from_env()
    summary = await repository.build_summary(
        window_hours=window_hours,
        degraded_latency_ms=settings.degraded_latency_ms,
    )
    print(summary.model_dump_json(indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pulsewatch", description="PulseWatch operations CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Create the SQLite schema")
    subparsers.add_parser("seed", help="Add safe public demo services")
    subparsers.add_parser("check", help="Check every active service now")
    summary_parser = subparsers.add_parser("summary", help="Print JSON metrics")
    summary_parser.add_argument("--hours", type=int, default=24)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "init-db":
        asyncio.run(init_database())
    elif args.command == "seed":
        asyncio.run(seed_database())
    elif args.command == "check":
        asyncio.run(run_all_checks())
    elif args.command == "summary":
        asyncio.run(print_summary(args.hours))


if __name__ == "__main__":
    main()
