"""Operational CLI for setup, checks, scheduling, retention, and credentials."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import signal
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.database import Database
from app.db_models import utc_now
from app.metrics import AppMetrics
from app.models import ServiceCreate
from app.monitoring import ApiMonitor
from app.notifications import WebhookNotifier
from app.repository import Repository
from app.scheduler import CheckScheduler

DEMO_SERVICES = (
    ServiceCreate(
        name="GitHub API",
        description="Public GitHub API availability",
        url="https://api.github.com",
        expected_status=200,
        timeout_seconds=5,
        retry_count=2,
        check_interval_seconds=300,
        tags=["public-api", "demo"],
    ),
    ServiceCreate(
        name="JSONPlaceholder",
        description="Public test API",
        url="https://jsonplaceholder.typicode.com/posts/1",
        expected_status=200,
        timeout_seconds=5,
        retry_count=2,
        check_interval_seconds=300,
        tags=["public-api", "demo"],
    ),
)


def build_components(
    settings: Settings | None = None,
) -> tuple[Settings, Database, Repository, ApiMonitor, CheckScheduler]:
    runtime_settings = settings or Settings.from_env()
    database = Database(runtime_settings.database_url)
    repository = Repository(database)
    metrics = AppMetrics(enabled=runtime_settings.metrics_enabled)
    notifier = WebhookNotifier(repository, runtime_settings, metrics)
    monitor = ApiMonitor(repository, runtime_settings, notifier, metrics)
    scheduler = CheckScheduler(repository, monitor, runtime_settings, metrics)
    return runtime_settings, database, repository, monitor, scheduler


async def init_database() -> None:
    _, database, _, _, _ = build_components()
    try:
        await database.initialize()
        print("Database schema is ready")
    finally:
        await database.dispose()


async def seed_database() -> None:
    _, database, repository, _, _ = build_components()
    await database.initialize()
    created = 0
    try:
        for service in DEMO_SERVICES:
            try:
                await repository.create_service(service, actor="cli")
                created += 1
                print(f"Added: {service.name}")
            except IntegrityError:
                print(f"Already exists: {service.name}")
        print(f"Seed complete: {created} service(s) added")
    finally:
        await database.dispose()


async def run_all_checks() -> None:
    _, database, _, monitor, _ = build_components()
    await database.initialize()
    try:
        results = await monitor.run_checks(trigger="manual", request_id="cli")
        if not results:
            print("No active services. Run pulsewatch seed first.")
            return
        for result in results:
            marker = "OK" if result.is_healthy else "FAIL"
            latency = (
                f"{result.latency_ms:.2f} ms"
                if result.latency_ms is not None
                else "n/a"
            )
            print(
                f"[{marker}] service={result.service_id} "
                f"status={result.status_code} latency={latency} "
                f"attempts={result.attempts}"
            )
    finally:
        await database.dispose()


async def print_summary(window_hours: int) -> None:
    settings, database, repository, _, _ = build_components()
    await database.initialize()
    try:
        summary = await repository.build_summary(
            window_hours=window_hours,
            degraded_latency_ms=settings.degraded_latency_ms,
        )
        print(summary.model_dump_json(indent=2))
    finally:
        await database.dispose()


async def run_scheduler_once() -> None:
    _, database, _, _, scheduler = build_components()
    await database.initialize()
    try:
        executed = await scheduler.run_once()
        print(f"Scheduler cycle complete: {executed} check(s) executed")
    finally:
        await database.dispose()


async def run_retention() -> None:
    settings, database, repository, _, _ = build_components()
    await database.initialize()
    cutoff = utc_now() - timedelta(days=settings.retention_days)
    try:
        deleted = await repository.purge_history(before=cutoff)
        print(f"Retention complete: {deleted} check result(s) removed")
    finally:
        await database.dispose()


async def run_worker() -> None:
    configured = Settings.from_env()
    settings = replace(configured, scheduler_enabled=True)
    _, database, _, _, scheduler = build_components(settings)
    if settings.auto_create_schema:
        await database.initialize()
    if not await database.ping() or not await database.schema_ready():
        raise RuntimeError("Database readiness check failed")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        stop_event.set()

    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, request_stop)

    await scheduler.start()
    print("PulseWatch scheduler worker started. Press Ctrl+C to stop.")
    try:
        await stop_event.wait()
    finally:
        await scheduler.stop()
        await database.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pulsewatch", description="PulseWatch operations CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Create the database schema")
    subparsers.add_parser("seed", help="Add safe public demo services")
    subparsers.add_parser("check", help="Check every active service now")
    summary_parser = subparsers.add_parser("summary", help="Print JSON metrics")
    summary_parser.add_argument("--hours", type=int, default=24)
    subparsers.add_parser("scheduler-once", help="Run one due-service cycle")
    subparsers.add_parser("worker", help="Run the scheduler until interrupted")
    subparsers.add_parser("retention", help="Delete expired check history")
    subparsers.add_parser("generate-key", help="Generate a secure admin API key")
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
    elif args.command == "scheduler-once":
        asyncio.run(run_scheduler_once())
    elif args.command == "worker":
        asyncio.run(run_worker())
    elif args.command == "retention":
        asyncio.run(run_retention())
    elif args.command == "generate-key":
        print(secrets.token_urlsafe(32))


if __name__ == "__main__":
    main()
