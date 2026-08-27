from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import update

from app.config import Settings
from app.db_models import CheckResultRow, utc_now
from app.metrics import AppMetrics
from app.models import ServiceCreate
from app.monitoring import ApiMonitor
from app.notifications import WebhookNotifier
from app.repository import Repository
from app.scheduler import CheckScheduler


def build_scheduler(
    repository: Repository,
    settings: Settings,
    handler: httpx.AsyncBaseTransport,
) -> CheckScheduler:
    metrics = AppMetrics()
    notifier = WebhookNotifier(repository, settings, metrics)
    monitor = ApiMonitor(
        repository,
        settings,
        notifier,
        metrics,
        transport=handler,
    )
    return CheckScheduler(repository, monitor, settings, metrics)


@pytest.mark.asyncio
async def test_run_once_checks_only_due_services(
    repository: Repository, settings: Settings
) -> None:
    service = await repository.create_service(
        ServiceCreate(
            name="Scheduled",
            url="https://scheduled.example/health",
            retry_count=0,
        )
    )
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    scheduler = build_scheduler(repository, settings, httpx.MockTransport(handler))
    assert await scheduler.run_once() == 1
    assert await scheduler.run_once() == 0
    assert calls == 1
    status = scheduler.status()
    assert status.checks_executed == 1
    assert status.last_cycle_started_at is not None
    assert status.last_cycle_finished_at is not None
    assert status.last_error is None
    assert status.retention_last_run_at is not None
    checks = await repository.list_check_results(
        service_id=service.id, trigger="scheduled"
    )
    assert len(checks) == 1


@pytest.mark.asyncio
async def test_background_scheduler_starts_cycles_and_stops(
    repository: Repository, settings: Settings
) -> None:
    configured = replace(
        settings,
        scheduler_enabled=True,
        scheduler_poll_seconds=0.01,
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    scheduler = build_scheduler(repository, configured, httpx.MockTransport(handler))
    await scheduler.start()
    await scheduler.start()
    assert scheduler.running is True
    assert scheduler.status().enabled is True
    await asyncio.sleep(0.025)
    await scheduler.stop()
    assert scheduler.running is False
    await scheduler.stop()
    assert scheduler.status().last_cycle_started_at is not None
    persisted = await repository.get_scheduler_status()
    assert persisted is not None
    assert persisted.enabled is True
    assert persisted.running is False
    assert persisted.heartbeat_at is not None


@pytest.mark.asyncio
async def test_disabled_scheduler_does_not_start(
    repository: Repository, settings: Settings
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    scheduler = build_scheduler(repository, settings, httpx.MockTransport(handler))
    await scheduler.start()
    assert scheduler.running is False
    await scheduler.stop()


@pytest.mark.asyncio
async def test_overlapping_manual_cycles_are_deduplicated(
    repository: Repository, settings: Settings
) -> None:
    await repository.create_service(
        ServiceCreate(name="Lock", url="https://lock.example/health")
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.025)
        return httpx.Response(200)

    scheduler = build_scheduler(repository, settings, httpx.MockTransport(handler))
    first, second = await asyncio.gather(
        scheduler.run_once(),
        scheduler.run_once(),
    )
    assert sorted([first, second]) == [0, 1]
    assert scheduler.checks_executed == 1


@pytest.mark.asyncio
async def test_cycle_failure_is_recorded_not_raised(
    repository: Repository, settings: Settings
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    scheduler = build_scheduler(repository, settings, httpx.MockTransport(handler))
    repository.list_services = AsyncMock(side_effect=RuntimeError("database offline"))
    assert await scheduler.run_once() == 0
    assert scheduler.last_error == "database offline"
    assert 'outcome="failed"' in scheduler.metrics.render().decode()


@pytest.mark.asyncio
async def test_scheduler_retention_removes_expired_results(
    repository: Repository, settings: Settings
) -> None:
    service = await repository.create_service(
        ServiceCreate(name="Old result", url="https://old.example/health")
    )
    result = await repository.record_check_result(
        service=service,
        status_code=200,
        latency_ms=10,
        is_healthy=True,
        attempts=1,
        trigger="manual",
    )
    async with repository.database.session() as session, session.begin():
        await session.execute(
            update(CheckResultRow)
            .where(CheckResultRow.id == result.result.id)
            .values(checked_at=utc_now() - timedelta(days=40))
        )

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    configured = replace(settings, retention_days=30)
    scheduler = build_scheduler(repository, configured, httpx.MockTransport(handler))
    await scheduler.run_once()
    assert await repository.list_check_results() == []
    assert scheduler.retention_last_run_at is not None
