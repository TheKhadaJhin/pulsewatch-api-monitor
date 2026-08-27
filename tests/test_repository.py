from __future__ import annotations

import pytest

from app.models import ServiceCreate, ServiceUpdate
from app.repository import Repository


@pytest.mark.asyncio
async def test_summary_aggregates_uptime_incidents_and_latency(
    repository: Repository,
) -> None:
    service = await repository.create_service(
        ServiceCreate(name="Payments", url="https://payments.example/health")
    )
    await repository.create_service(
        ServiceCreate(name="No history", url="https://empty.example/health")
    )

    await repository.add_check_result(
        service_id=service.id,
        status_code=200,
        latency_ms=100,
        is_healthy=True,
        attempts=1,
    )
    await repository.add_check_result(
        service_id=service.id,
        status_code=503,
        latency_ms=300,
        is_healthy=False,
        attempts=3,
        error_type="UnexpectedStatus",
        error_message="Expected HTTP 200, received 503",
    )

    summary = await repository.build_summary(window_hours=24, degraded_latency_ms=200)

    assert summary.services_total == 2
    assert summary.checks_total == 2
    assert summary.incidents_total == 1
    assert summary.uptime_percentage == 50.0
    assert summary.average_latency_ms == 200.0
    assert summary.down_services == 1
    assert summary.never_checked == 1
    assert summary.services[1].uptime_percentage == 50.0


@pytest.mark.asyncio
async def test_slow_success_is_classified_as_degraded(
    repository: Repository,
) -> None:
    service = await repository.create_service(
        ServiceCreate(name="Slow", url="https://slow.example/health")
    )
    await repository.add_check_result(
        service_id=service.id,
        status_code=200,
        latency_ms=900,
        is_healthy=True,
        attempts=1,
    )
    summary = await repository.build_summary(window_hours=24, degraded_latency_ms=750)
    assert summary.degraded_services == 1
    assert summary.services[0].state == "degraded"


@pytest.mark.asyncio
async def test_inactive_services_are_excluded_from_summary(
    repository: Repository,
) -> None:
    service = await repository.create_service(
        ServiceCreate(name="Disabled", url="https://disabled.example/health")
    )
    updated = await repository.update_service(service.id, ServiceUpdate(active=False))
    assert updated is not None and updated.active is False

    summary = await repository.build_summary(window_hours=24, degraded_latency_ms=750)
    assert summary.services_total == 0
    assert summary.uptime_percentage is None
    assert summary.average_latency_ms is None


@pytest.mark.asyncio
async def test_list_services_preserves_requested_filter(repository: Repository) -> None:
    first = await repository.create_service(
        ServiceCreate(name="First", url="https://first.example/health")
    )
    await repository.create_service(
        ServiceCreate(name="Second", url="https://second.example/health")
    )
    selected = await repository.list_services(service_ids=[first.id])
    assert [service.id for service in selected] == [first.id]
    assert await repository.list_services(service_ids=[]) == []
