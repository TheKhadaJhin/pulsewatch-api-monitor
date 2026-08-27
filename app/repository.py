"""Database repository, incident lifecycle, auditing, and metrics queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Database
from app.db_models import (
    AuditEventRow,
    CheckResultRow,
    IncidentRow,
    NotificationDeliveryRow,
    SchedulerStateRow,
    ServiceRow,
    utc_now,
)
from app.models import (
    AuditEventOut,
    CheckResultOut,
    CheckTrigger,
    IncidentEventType,
    IncidentOut,
    IncidentTransition,
    NotificationDeliveryOut,
    SchedulerStatusOut,
    ServiceCreate,
    ServiceMetric,
    ServiceOut,
    ServiceUpdate,
    SummaryOut,
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _service_out(row: ServiceRow) -> ServiceOut:
    return ServiceOut(
        id=row.id,
        name=row.name,
        description=row.description,
        url=row.url,
        method=row.method,  # type: ignore[arg-type]
        expected_status=row.expected_status,
        timeout_seconds=row.timeout_seconds,
        retry_count=row.retry_count,
        check_interval_seconds=row.check_interval_seconds,
        active=row.active,
        tags=list(row.tags or []),
        next_check_at=_aware(row.next_check_at),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def _check_result_out(row: CheckResultRow) -> CheckResultOut:
    return CheckResultOut(
        id=row.id,
        service_id=row.service_id,
        checked_at=_aware(row.checked_at),
        status_code=row.status_code,
        latency_ms=row.latency_ms,
        is_healthy=row.is_healthy,
        attempts=row.attempts,
        trigger=row.trigger,  # type: ignore[arg-type]
        error_type=row.error_type,
        error_message=row.error_message,
        request_id=row.request_id,
    )


def _incident_out(row: IncidentRow, service_name: str) -> IncidentOut:
    opened_at = _aware(row.opened_at)
    resolved_at = _aware(row.resolved_at) if row.resolved_at else None
    return IncidentOut(
        id=row.id,
        service_id=row.service_id,
        service_name=service_name,
        status=row.status,  # type: ignore[arg-type]
        opened_at=opened_at,
        resolved_at=resolved_at,
        last_failure_at=_aware(row.last_failure_at),
        duration_seconds=(
            round((resolved_at - opened_at).total_seconds(), 2) if resolved_at else None
        ),
        failure_count=row.failure_count,
        initial_error_type=row.initial_error_type,
        initial_error_message=row.initial_error_message,
        last_error_type=row.last_error_type,
        last_error_message=row.last_error_message,
    )


def _notification_out(row: NotificationDeliveryRow) -> NotificationDeliveryOut:
    return NotificationDeliveryOut(
        id=row.id,
        incident_id=row.incident_id,
        event_type=row.event_type,  # type: ignore[arg-type]
        attempted_at=_aware(row.attempted_at),
        delivered=row.delivered,
        status_code=row.status_code,
        attempts=row.attempts,
        error_message=row.error_message,
    )


@dataclass(frozen=True, slots=True)
class RecordedCheck:
    result: CheckResultOut
    transition: IncidentTransition | None


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def _audit(
        self,
        session: AsyncSession,
        *,
        action: str,
        entity_type: str,
        entity_id: str | None,
        actor: str,
        request_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            AuditEventRow(
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                actor=actor,
                request_id=request_id,
                event_metadata=metadata or {},
            )
        )

    async def create_service(
        self,
        payload: ServiceCreate,
        *,
        actor: str = "api",
        request_id: str | None = None,
    ) -> ServiceOut:
        values = payload.model_dump()
        row = ServiceRow(**values, next_check_at=utc_now())
        async with self.database.session() as session, session.begin():
            session.add(row)
            await session.flush()
            await self._audit(
                session,
                action="service.created",
                entity_type="service",
                entity_id=str(row.id),
                actor=actor,
                request_id=request_id,
                metadata={"name": row.name, "url": row.url},
            )
        return _service_out(row)

    async def list_services(
        self,
        *,
        active_only: bool = False,
        service_ids: list[int] | None = None,
        due_before: datetime | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[ServiceOut]:
        statement = select(ServiceRow)
        if active_only:
            statement = statement.where(ServiceRow.active.is_(True))
        if service_ids is not None:
            if not service_ids:
                return []
            statement = statement.where(ServiceRow.id.in_(service_ids))
        if due_before is not None:
            statement = statement.where(ServiceRow.next_check_at <= due_before)
        statement = (
            statement.order_by(func.lower(ServiceRow.name), ServiceRow.id)
            .limit(limit)
            .offset(offset)
        )
        async with self.database.session() as session:
            rows = (await session.scalars(statement)).all()
        return [_service_out(row) for row in rows]

    async def get_service(self, service_id: int) -> ServiceOut | None:
        async with self.database.session() as session:
            row = await session.get(ServiceRow, service_id)
        return None if row is None else _service_out(row)

    async def update_service(
        self,
        service_id: int,
        payload: ServiceUpdate,
        *,
        actor: str = "api",
        request_id: str | None = None,
    ) -> ServiceOut | None:
        values = payload.model_dump(exclude_unset=True)
        async with self.database.session() as session, session.begin():
            row = await session.get(ServiceRow, service_id)
            if row is None:
                return None
            for field, value in values.items():
                setattr(row, field, value)
            row.updated_at = utc_now()
            if values.get("active") is True or "check_interval_seconds" in values:
                row.next_check_at = utc_now()
            await self._audit(
                session,
                action="service.updated",
                entity_type="service",
                entity_id=str(service_id),
                actor=actor,
                request_id=request_id,
                metadata={"changed_fields": sorted(values)},
            )
        return _service_out(row)

    async def delete_service(
        self,
        service_id: int,
        *,
        actor: str = "api",
        request_id: str | None = None,
    ) -> bool:
        async with self.database.session() as session, session.begin():
            row = await session.get(ServiceRow, service_id)
            if row is None:
                return False
            name = row.name
            await session.delete(row)
            await self._audit(
                session,
                action="service.deleted",
                entity_type="service",
                entity_id=str(service_id),
                actor=actor,
                request_id=request_id,
                metadata={"name": name},
            )
        return True

    async def record_check_result(
        self,
        *,
        service: ServiceOut,
        status_code: int | None,
        latency_ms: float | None,
        is_healthy: bool,
        attempts: int,
        trigger: CheckTrigger,
        error_type: str | None = None,
        error_message: str | None = None,
        request_id: str | None = None,
    ) -> RecordedCheck:
        now = utc_now()
        result_row = CheckResultRow(
            service_id=service.id,
            checked_at=now,
            status_code=status_code,
            latency_ms=latency_ms,
            is_healthy=is_healthy,
            attempts=attempts,
            trigger=trigger,
            error_type=error_type,
            error_message=error_message,
            request_id=request_id,
        )
        transition: IncidentTransition | None = None

        async with self.database.session() as session, session.begin():
            # Lock the parent first so concurrent API/worker checks for one service
            # cannot both observe that no incident exists and open duplicates.
            service_row = await session.scalar(
                select(ServiceRow).where(ServiceRow.id == service.id).with_for_update()
            )
            if service_row is None:
                raise LookupError(
                    f"Service {service.id} was deleted before its check was recorded"
                )
            session.add(result_row)
            await session.flush()
            open_statement = (
                select(IncidentRow)
                .where(
                    IncidentRow.service_id == service.id,
                    IncidentRow.status == "open",
                )
                .order_by(IncidentRow.id.desc())
                .limit(1)
                .with_for_update()
            )
            incident = (await session.scalars(open_statement)).first()

            if not is_healthy:
                if incident is None:
                    incident = IncidentRow(
                        service_id=service.id,
                        status="open",
                        opened_at=now,
                        last_failure_at=now,
                        failure_count=1,
                        initial_error_type=error_type,
                        initial_error_message=error_message,
                        last_error_type=error_type,
                        last_error_message=error_message,
                    )
                    session.add(incident)
                    await session.flush()
                    transition = IncidentTransition(
                        event_type="incident_opened",
                        incident=_incident_out(incident, service.name),
                        service=service,
                    )
                else:
                    incident.failure_count += 1
                    incident.last_failure_at = now
                    incident.last_error_type = error_type
                    incident.last_error_message = error_message
            elif incident is not None:
                incident.status = "resolved"
                incident.resolved_at = now
                await session.flush()
                transition = IncidentTransition(
                    event_type="incident_resolved",
                    incident=_incident_out(incident, service.name),
                    service=service,
                )

            service_row.next_check_at = now + timedelta(
                seconds=service_row.check_interval_seconds
            )

        return RecordedCheck(
            result=_check_result_out(result_row),
            transition=transition,
        )

    async def list_check_results(
        self,
        *,
        service_id: int | None = None,
        trigger: CheckTrigger | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CheckResultOut]:
        statement = select(CheckResultRow)
        if service_id is not None:
            statement = statement.where(CheckResultRow.service_id == service_id)
        if trigger is not None:
            statement = statement.where(CheckResultRow.trigger == trigger)
        statement = (
            statement.order_by(
                CheckResultRow.checked_at.desc(), CheckResultRow.id.desc()
            )
            .limit(limit)
            .offset(offset)
        )
        async with self.database.session() as session:
            rows = (await session.scalars(statement)).all()
        return [_check_result_out(row) for row in rows]

    async def list_incidents(
        self,
        *,
        status: str | None = None,
        service_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[IncidentOut]:
        statement = select(IncidentRow, ServiceRow.name).join(ServiceRow)
        if status is not None:
            statement = statement.where(IncidentRow.status == status)
        if service_id is not None:
            statement = statement.where(IncidentRow.service_id == service_id)
        statement = (
            statement.order_by(IncidentRow.opened_at.desc(), IncidentRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self.database.session() as session:
            rows = (await session.execute(statement)).all()
        return [_incident_out(incident, name) for incident, name in rows]

    async def record_notification_delivery(
        self,
        *,
        incident_id: int,
        event_type: IncidentEventType,
        delivered: bool,
        status_code: int | None,
        attempts: int,
        error_message: str | None,
    ) -> NotificationDeliveryOut:
        row = NotificationDeliveryRow(
            incident_id=incident_id,
            event_type=event_type,
            delivered=delivered,
            status_code=status_code,
            attempts=attempts,
            error_message=error_message,
        )
        async with self.database.session() as session, session.begin():
            session.add(row)
            await session.flush()
        return _notification_out(row)

    async def list_notification_deliveries(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[NotificationDeliveryOut]:
        statement = (
            select(NotificationDeliveryRow)
            .order_by(
                NotificationDeliveryRow.attempted_at.desc(),
                NotificationDeliveryRow.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        async with self.database.session() as session:
            rows = (await session.scalars(statement)).all()
        return [_notification_out(row) for row in rows]

    async def save_scheduler_status(self, status: SchedulerStatusOut) -> None:
        values = status.model_dump()
        async with self.database.session() as session, session.begin():
            row = await session.get(SchedulerStateRow, 1)
            if row is None:
                row = SchedulerStateRow(id=1, **values)
                session.add(row)
            else:
                for field, value in values.items():
                    setattr(row, field, value)
                row.updated_at = utc_now()

    async def get_scheduler_status(self) -> SchedulerStatusOut | None:
        async with self.database.session() as session:
            row = await session.get(SchedulerStateRow, 1)
        if row is None:
            return None

        heartbeat = _aware(row.heartbeat_at) if row.heartbeat_at else None
        stale_after_seconds = max(5.0, row.poll_seconds * 3)
        running = row.running
        last_error = row.last_error
        if heartbeat is None:
            running = False
        elif running and heartbeat < utc_now() - timedelta(seconds=stale_after_seconds):
            running = False
            last_error = last_error or "Scheduler heartbeat is stale"

        return SchedulerStatusOut(
            enabled=row.enabled,
            running=running,
            cycle_in_progress=row.cycle_in_progress if running else False,
            poll_seconds=row.poll_seconds,
            heartbeat_at=heartbeat,
            last_cycle_started_at=(
                _aware(row.last_cycle_started_at) if row.last_cycle_started_at else None
            ),
            last_cycle_finished_at=(
                _aware(row.last_cycle_finished_at)
                if row.last_cycle_finished_at
                else None
            ),
            last_error=last_error,
            checks_executed=row.checks_executed,
            retention_last_run_at=(
                _aware(row.retention_last_run_at) if row.retention_last_run_at else None
            ),
        )

    async def record_audit_event(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str | None,
        actor: str,
        request_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self.database.session() as session, session.begin():
            await self._audit(
                session,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                actor=actor,
                request_id=request_id,
                metadata=metadata,
            )

    async def list_audit_events(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[AuditEventOut]:
        statement = (
            select(AuditEventRow)
            .order_by(AuditEventRow.occurred_at.desc(), AuditEventRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self.database.session() as session:
            rows = (await session.scalars(statement)).all()
        return [
            AuditEventOut(
                id=row.id,
                occurred_at=_aware(row.occurred_at),
                action=row.action,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                actor=row.actor,
                request_id=row.request_id,
                metadata=dict(row.event_metadata or {}),
            )
            for row in rows
        ]

    async def purge_history(self, *, before: datetime) -> int:
        async with self.database.session() as session, session.begin():
            result = await session.execute(
                delete(CheckResultRow).where(CheckResultRow.checked_at < before)
            )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def build_summary(
        self, *, window_hours: int, degraded_latency_ms: float
    ) -> SummaryOut:
        cutoff = utc_now() - timedelta(hours=window_hours)
        recent = (
            select(
                CheckResultRow.service_id,
                CheckResultRow.is_healthy,
                CheckResultRow.latency_ms,
            )
            .where(CheckResultRow.checked_at >= cutoff)
            .subquery()
        )
        stats = (
            select(
                recent.c.service_id.label("service_id"),
                func.count().label("checks"),
                func.sum(case((recent.c.is_healthy.is_(True), 1), else_=0)).label(
                    "healthy_checks"
                ),
                func.avg(recent.c.latency_ms).label("average_latency_ms"),
                func.count(recent.c.latency_ms).label("latency_count"),
            )
            .group_by(recent.c.service_id)
            .subquery()
        )
        ranked = select(
            CheckResultRow.service_id,
            CheckResultRow.checked_at,
            CheckResultRow.status_code,
            CheckResultRow.latency_ms,
            CheckResultRow.is_healthy,
            func.row_number()
            .over(
                partition_by=CheckResultRow.service_id,
                order_by=(
                    CheckResultRow.checked_at.desc(),
                    CheckResultRow.id.desc(),
                ),
            )
            .label("row_number"),
        ).subquery()
        latest = select(ranked).where(ranked.c.row_number == 1).subquery()
        statement = (
            select(
                ServiceRow,
                stats.c.checks,
                stats.c.healthy_checks,
                stats.c.average_latency_ms,
                stats.c.latency_count,
                latest.c.checked_at.label("last_checked_at"),
                latest.c.status_code.label("last_status_code"),
                latest.c.latency_ms.label("last_latency_ms"),
                latest.c.is_healthy.label("last_is_healthy"),
            )
            .outerjoin(stats, stats.c.service_id == ServiceRow.id)
            .outerjoin(latest, latest.c.service_id == ServiceRow.id)
            .where(ServiceRow.active.is_(True))
            .order_by(func.lower(ServiceRow.name), ServiceRow.id)
        )

        async with self.database.session() as session:
            rows = (await session.execute(statement)).all()
            active_incidents = (
                select(
                    func.sum(case((IncidentRow.opened_at >= cutoff, 1), else_=0)).label(
                        "window_total"
                    ),
                    func.sum(case((IncidentRow.status == "open", 1), else_=0)).label(
                        "open_total"
                    ),
                )
                .join(ServiceRow)
                .where(ServiceRow.active.is_(True))
            )
            incident_counts = (await session.execute(active_incidents)).one()
            incidents_total = int(incident_counts.window_total or 0)
            open_incidents = int(incident_counts.open_total or 0)

        services: list[ServiceMetric] = []
        checks_total = 0
        healthy_checks_total = 0
        latency_weighted_sum = 0.0
        latency_observations = 0
        state_counts = {
            "healthy": 0,
            "degraded": 0,
            "down": 0,
            "never_checked": 0,
        }

        for row in rows:
            service = row[0]
            checks = int(row.checks or 0)
            healthy_checks = int(row.healthy_checks or 0)
            latency_count = int(row.latency_count or 0)
            last_is_healthy = row.last_is_healthy
            last_latency = row.last_latency_ms

            if last_is_healthy is None:
                state = "never_checked"
            elif not bool(last_is_healthy):
                state = "down"
            elif last_latency is not None and last_latency > degraded_latency_ms:
                state = "degraded"
            else:
                state = "healthy"

            state_counts[state] += 1
            checks_total += checks
            healthy_checks_total += healthy_checks
            if row.average_latency_ms is not None and latency_count:
                latency_weighted_sum += float(row.average_latency_ms) * latency_count
                latency_observations += latency_count

            services.append(
                ServiceMetric(
                    id=service.id,
                    name=service.name,
                    url=service.url,
                    state=state,  # type: ignore[arg-type]
                    last_checked_at=(
                        _aware(row.last_checked_at) if row.last_checked_at else None
                    ),
                    last_status_code=row.last_status_code,
                    last_latency_ms=(
                        round(float(last_latency), 2)
                        if last_latency is not None
                        else None
                    ),
                    next_check_at=_aware(service.next_check_at),
                    checks=checks,
                    uptime_percentage=(
                        round(healthy_checks * 100 / checks, 2) if checks else None
                    ),
                )
            )

        return SummaryOut(
            generated_at=utc_now(),
            window_hours=window_hours,
            services_total=len(services),
            healthy_services=state_counts["healthy"],
            degraded_services=state_counts["degraded"],
            down_services=state_counts["down"],
            never_checked=state_counts["never_checked"],
            checks_total=checks_total,
            incidents_total=incidents_total,
            open_incidents=open_incidents,
            uptime_percentage=(
                round(healthy_checks_total * 100 / checks_total, 2)
                if checks_total
                else None
            ),
            average_latency_ms=(
                round(latency_weighted_sum / latency_observations, 2)
                if latency_observations
                else None
            ),
            services=services,
        )
