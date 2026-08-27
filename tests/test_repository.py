from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update

from app.db_models import CheckResultRow, utc_now
from app.models import SchedulerStatusOut, ServiceCreate, ServiceOut, ServiceUpdate
from app.repository import RecordedCheck, Repository


async def record(
    repository: Repository,
    service: ServiceOut,
    *,
    healthy: bool,
    latency_ms: float = 100,
    trigger: str = "manual",
) -> RecordedCheck:
    return await repository.record_check_result(
        service=service,
        status_code=200 if healthy else 503,
        latency_ms=latency_ms,
        is_healthy=healthy,
        attempts=1 if healthy else 3,
        trigger=trigger,  # type: ignore[arg-type]
        error_type=None if healthy else "UnexpectedStatus",
        error_message=None if healthy else "Expected HTTP 200, received 503",
        request_id="repository-test",
    )


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

    await record(repository, service, healthy=True, latency_ms=100)
    await record(repository, service, healthy=False, latency_ms=300)
    summary = await repository.build_summary(window_hours=24, degraded_latency_ms=200)

    assert summary.services_total == 2
    assert summary.checks_total == 2
    assert summary.incidents_total == 1
    assert summary.open_incidents == 1
    assert summary.uptime_percentage == 50.0
    assert summary.average_latency_ms == 200.0
    assert summary.down_services == 1
    assert summary.never_checked == 1
    metric = next(item for item in summary.services if item.id == service.id)
    assert metric.uptime_percentage == 50.0
    assert metric.last_status_code == 503


@pytest.mark.asyncio
async def test_slow_success_is_classified_as_degraded(
    repository: Repository,
) -> None:
    service = await repository.create_service(
        ServiceCreate(name="Slow", url="https://slow.example/health")
    )
    await record(repository, service, healthy=True, latency_ms=900)
    summary = await repository.build_summary(window_hours=24, degraded_latency_ms=750)
    assert summary.degraded_services == 1
    assert summary.services[0].state == "degraded"
    assert summary.services[0].last_latency_ms == 900


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
async def test_service_filters_order_pagination_and_due_schedule(
    repository: Repository,
) -> None:
    second = await repository.create_service(
        ServiceCreate(name="Zulu", url="https://zulu.example/health")
    )
    first = await repository.create_service(
        ServiceCreate(name="Alpha", url="https://alpha.example/health")
    )
    ordered = await repository.list_services()
    assert [service.name for service in ordered] == ["Alpha", "Zulu"]
    assert [service.id for service in await repository.list_services(limit=1)] == [
        first.id
    ]
    assert [
        service.id for service in await repository.list_services(limit=1, offset=1)
    ] == [second.id]
    selected = await repository.list_services(service_ids=[second.id])
    assert [service.id for service in selected] == [second.id]
    assert await repository.list_services(service_ids=[]) == []

    due = await repository.list_services(
        active_only=True, due_before=utc_now() + timedelta(seconds=1)
    )
    assert {service.id for service in due} == {first.id, second.id}
    await record(repository, first, healthy=True, trigger="scheduled")
    no_longer_due = await repository.list_services(
        active_only=True, due_before=utc_now() + timedelta(seconds=1)
    )
    assert [service.id for service in no_longer_due] == [second.id]


@pytest.mark.asyncio
async def test_update_reactivates_and_reschedules_service(
    repository: Repository,
) -> None:
    service = await repository.create_service(
        ServiceCreate(name="Toggle", url="https://toggle.example/health")
    )
    assert await repository.update_service(999, ServiceUpdate(active=False)) is None
    disabled = await repository.update_service(service.id, ServiceUpdate(active=False))
    assert disabled is not None and disabled.active is False
    reactivated = await repository.update_service(
        service.id,
        ServiceUpdate(active=True, check_interval_seconds=30, description="Fast cycle"),
    )
    assert reactivated is not None
    assert reactivated.active is True
    assert reactivated.check_interval_seconds == 30
    assert reactivated.description == "Fast cycle"
    assert reactivated.updated_at >= service.updated_at


@pytest.mark.asyncio
async def test_incident_lifecycle_opens_updates_and_resolves(
    repository: Repository,
) -> None:
    service = await repository.create_service(
        ServiceCreate(name="Checkout", url="https://checkout.example/health")
    )
    first_failure = await record(repository, service, healthy=False)
    assert first_failure.transition is not None
    assert first_failure.transition.event_type == "incident_opened"
    assert first_failure.transition.incident.failure_count == 1

    second_failure = await record(repository, service, healthy=False)
    assert second_failure.transition is None
    opened = (await repository.list_incidents(status="open", service_id=service.id))[0]
    assert opened.failure_count == 2
    assert opened.last_error_type == "UnexpectedStatus"
    summary = await repository.build_summary(window_hours=24, degraded_latency_ms=750)
    assert summary.incidents_total == 1

    recovery = await record(repository, service, healthy=True)
    assert recovery.transition is not None
    assert recovery.transition.event_type == "incident_resolved"
    resolved = (await repository.list_incidents(status="resolved"))[0]
    assert resolved.resolved_at is not None
    assert resolved.duration_seconds is not None
    assert await repository.list_incidents(status="open") == []

    stable = await record(repository, service, healthy=True)
    assert stable.transition is None

    later_failure = await record(repository, service, healthy=False)
    assert later_failure.transition is not None
    assert later_failure.transition.event_type == "incident_opened"
    all_incidents = await repository.list_incidents(service_id=service.id)
    assert len(all_incidents) == 2
    assert sum(incident.status == "open" for incident in all_incidents) == 1


@pytest.mark.asyncio
async def test_check_incident_and_audit_history_support_pagination(
    repository: Repository,
) -> None:
    service = await repository.create_service(
        ServiceCreate(name="History", url="https://history.example/health"),
        actor="mario",
        request_id="create-1",
    )
    await record(repository, service, healthy=True)
    await record(repository, service, healthy=False, trigger="scheduled")

    all_checks = await repository.list_check_results(limit=10)
    assert len(all_checks) == 2
    assert len(await repository.list_check_results(limit=1, offset=1)) == 1
    scheduled = await repository.list_check_results(
        service_id=service.id, trigger="scheduled"
    )
    assert len(scheduled) == 1
    assert scheduled[0].request_id == "repository-test"

    await repository.record_audit_event(
        action="custom.action",
        entity_type="portfolio",
        entity_id="pulsewatch",
        actor="mario",
        request_id="audit-1",
        metadata={"professional": True},
    )
    audit = await repository.list_audit_events(limit=10)
    assert audit[0].action == "custom.action"
    assert audit[0].metadata == {"professional": True}
    assert len(await repository.list_audit_events(limit=1, offset=1)) == 1


@pytest.mark.asyncio
async def test_notification_delivery_is_persisted(repository: Repository) -> None:
    service = await repository.create_service(
        ServiceCreate(name="Alerts", url="https://alerts.example/health")
    )
    failure = await record(repository, service, healthy=False)
    assert failure.transition is not None
    incident_id = failure.transition.incident.id
    delivery = await repository.record_notification_delivery(
        incident_id=incident_id,
        event_type="incident_opened",
        delivered=False,
        status_code=503,
        attempts=3,
        error_message="Webhook returned HTTP 503",
    )
    assert delivery.incident_id == incident_id
    assert delivery.delivered is False
    history = await repository.list_notification_deliveries()
    assert history[0].attempts == 3
    assert len(await repository.list_notification_deliveries(limit=1, offset=0)) == 1


@pytest.mark.asyncio
async def test_retention_deletes_only_expired_results(repository: Repository) -> None:
    service = await repository.create_service(
        ServiceCreate(name="Retention", url="https://retention.example/health")
    )
    old = await record(repository, service, healthy=True)
    recent = await record(repository, service, healthy=True)
    async with repository.database.session() as session, session.begin():
        await session.execute(
            update(CheckResultRow)
            .where(CheckResultRow.id == old.result.id)
            .values(checked_at=utc_now() - timedelta(days=40))
        )

    deleted = await repository.purge_history(before=utc_now() - timedelta(days=30))
    assert deleted == 1
    remaining = await repository.list_check_results()
    assert [item.id for item in remaining] == [recent.result.id]


@pytest.mark.asyncio
async def test_delete_and_database_lifecycle(repository: Repository) -> None:
    service = await repository.create_service(
        ServiceCreate(name="Delete", url="https://delete.example/health")
    )
    assert await repository.get_service(service.id) is not None
    assert await repository.delete_service(service.id, actor="mario") is True
    assert await repository.get_service(service.id) is None
    assert await repository.delete_service(service.id) is False
    assert await repository.database.ping() is True
    assert await repository.database.schema_ready() is True
    await repository.database.drop_all()
    assert await repository.database.schema_ready() is False


@pytest.mark.asyncio
async def test_stale_check_cannot_recreate_deleted_service_history(
    repository: Repository,
) -> None:
    service = await repository.create_service(
        ServiceCreate(name="Race", url="https://race.example/health")
    )
    assert await repository.delete_service(service.id) is True
    with pytest.raises(LookupError, match="deleted before"):
        await record(repository, service, healthy=False)


@pytest.mark.asyncio
async def test_scheduler_heartbeat_is_persisted_and_staleness_is_detected(
    repository: Repository,
) -> None:
    assert await repository.get_scheduler_status() is None
    stale = SchedulerStatusOut(
        enabled=True,
        running=True,
        cycle_in_progress=True,
        poll_seconds=0.1,
        heartbeat_at=utc_now() - timedelta(seconds=10),
        last_cycle_started_at=utc_now() - timedelta(seconds=11),
        last_cycle_finished_at=None,
        last_error=None,
        checks_executed=12,
        retention_last_run_at=None,
    )
    await repository.save_scheduler_status(stale)
    loaded = await repository.get_scheduler_status()
    assert loaded is not None
    assert loaded.running is False
    assert loaded.cycle_in_progress is False
    assert loaded.last_error == "Scheduler heartbeat is stale"

    healthy = stale.model_copy(
        update={
            "heartbeat_at": utc_now(),
            "cycle_in_progress": False,
            "last_cycle_finished_at": utc_now(),
        }
    )
    await repository.save_scheduler_status(healthy)
    loaded = await repository.get_scheduler_status()
    assert loaded is not None
    assert loaded.running is True
    assert loaded.last_error is None
