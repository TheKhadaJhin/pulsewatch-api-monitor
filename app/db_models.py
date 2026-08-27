"""SQLAlchemy ORM tables shared by SQLite and PostgreSQL deployments."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ServiceRow(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(240))
    url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False, default="GET")
    expected_status: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    check_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    check_results: Mapped[list[CheckResultRow]] = relationship(
        back_populates="service", cascade="all, delete-orphan"
    )
    incidents: Mapped[list[IncidentRow]] = relationship(
        back_populates="service", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_services_active_next_check", "active", "next_check_at"),
        CheckConstraint("method IN ('GET', 'HEAD')", name="ck_services_method"),
        CheckConstraint(
            "expected_status BETWEEN 100 AND 599",
            name="ck_services_expected_status",
        ),
        CheckConstraint(
            "retry_count BETWEEN 0 AND 3",
            name="ck_services_retry_count",
        ),
        CheckConstraint(
            "timeout_seconds > 0.1 AND timeout_seconds <= 30",
            name="ck_services_timeout",
        ),
        CheckConstraint(
            "check_interval_seconds BETWEEN 30 AND 86400",
            name="ck_services_interval",
        ),
        UniqueConstraint("url", name="uq_services_url"),
    )


class CheckResultRow(Base):
    __tablename__ = "check_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
    status_code: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    is_healthy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    error_type: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(500))
    request_id: Mapped[str | None] = mapped_column(String(128))

    service: Mapped[ServiceRow] = relationship(back_populates="check_results")

    __table_args__ = (
        Index("idx_check_results_service_time", "service_id", "checked_at"),
        CheckConstraint(
            "trigger IN ('manual', 'scheduled')",
            name="ck_results_trigger",
        ),
        CheckConstraint(
            "status_code IS NULL OR status_code BETWEEN 100 AND 599",
            name="ck_results_status_code",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_results_latency",
        ),
        CheckConstraint(
            "attempts BETWEEN 1 AND 4",
            name="ck_results_attempts",
        ),
    )


class IncidentRow(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    initial_error_type: Mapped[str | None] = mapped_column(String(80))
    initial_error_message: Mapped[str | None] = mapped_column(String(500))
    last_error_type: Mapped[str | None] = mapped_column(String(80))
    last_error_message: Mapped[str | None] = mapped_column(String(500))

    service: Mapped[ServiceRow] = relationship(back_populates="incidents")
    deliveries: Mapped[list[NotificationDeliveryRow]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_incidents_status_opened", "status", "opened_at"),
        CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_incidents_status",
        ),
        CheckConstraint(
            "failure_count >= 1",
            name="ck_incidents_failure_count",
        ),
        CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL)",
            name="ck_incidents_resolution",
        ),
        Index(
            "uq_incidents_one_open_per_service",
            "service_id",
            unique=True,
            sqlite_where=text("status = 'open'"),
            postgresql_where=text("status = 'open'"),
        ),
    )


class NotificationDeliveryRow(Base):
    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(500))

    incident: Mapped[IncidentRow] = relationship(back_populates="deliveries")

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('incident_opened', 'incident_resolved')",
            name="ck_deliveries_event_type",
        ),
        CheckConstraint("attempts >= 1", name="ck_deliveries_attempts"),
        CheckConstraint(
            "status_code IS NULL OR status_code BETWEEN 100 AND 599",
            name="ck_deliveries_status_code",
        ),
    )


class SchedulerStateRow(Base):
    __tablename__ = "scheduler_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    running: Mapped[bool] = mapped_column(Boolean, nullable=False)
    cycle_in_progress: Mapped[bool] = mapped_column(Boolean, nullable=False)
    poll_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_cycle_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_cycle_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(String(500))
    checks_executed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retention_last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_scheduler_singleton"),
        CheckConstraint("poll_seconds > 0", name="ck_scheduler_poll"),
        CheckConstraint("checks_executed >= 0", name="ck_scheduler_checks"),
    )


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="system")
    request_id: Mapped[str | None] = mapped_column(String(128))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
