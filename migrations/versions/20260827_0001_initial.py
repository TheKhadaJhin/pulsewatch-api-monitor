"""Create the PulseWatch 2.0 operational schema.

Revision ID: 20260827_0001
Revises:
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "services",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=240), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("expected_status", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("check_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column(
            "next_check_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("method IN ('GET', 'HEAD')", name="ck_services_method"),
        sa.CheckConstraint(
            "expected_status BETWEEN 100 AND 599",
            name="ck_services_expected_status",
        ),
        sa.CheckConstraint(
            "retry_count BETWEEN 0 AND 3", name="ck_services_retry_count"
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0.1 AND timeout_seconds <= 30",
            name="ck_services_timeout",
        ),
        sa.CheckConstraint(
            "check_interval_seconds BETWEEN 30 AND 86400",
            name="ck_services_interval",
        ),
        sa.UniqueConstraint("url", name="uq_services_url"),
    )
    op.create_index(
        "idx_services_active_next_check",
        "services",
        ["active", "next_check_at"],
    )
    op.create_index("ix_services_next_check_at", "services", ["next_check_at"])

    op.create_table(
        "check_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("is_healthy", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("error_type", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('manual', 'scheduled')", name="ck_results_trigger"
        ),
        sa.CheckConstraint(
            "status_code IS NULL OR status_code BETWEEN 100 AND 599",
            name="ck_results_status_code",
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_results_latency",
        ),
        sa.CheckConstraint(
            "attempts BETWEEN 1 AND 4",
            name="ck_results_attempts",
        ),
    )
    op.create_index(
        "idx_check_results_service_time",
        "check_results",
        ["service_id", "checked_at"],
    )
    op.create_index("ix_check_results_checked_at", "check_results", ["checked_at"])
    op.create_index("ix_check_results_service_id", "check_results", ["service_id"])

    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_failure_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("initial_error_type", sa.String(length=80), nullable=True),
        sa.Column("initial_error_message", sa.String(length=500), nullable=True),
        sa.Column("last_error_type", sa.String(length=80), nullable=True),
        sa.Column("last_error_message", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')", name="ck_incidents_status"
        ),
        sa.CheckConstraint(
            "failure_count >= 1",
            name="ck_incidents_failure_count",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL)",
            name="ck_incidents_resolution",
        ),
    )
    op.create_index("idx_incidents_status_opened", "incidents", ["status", "opened_at"])
    op.create_index("ix_incidents_service_id", "incidents", ["service_id"])
    op.create_index(
        "uq_incidents_one_open_per_service",
        "incidents",
        ["service_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("delivered", sa.Boolean(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('incident_opened', 'incident_resolved')",
            name="ck_deliveries_event_type",
        ),
        sa.CheckConstraint(
            "attempts >= 1",
            name="ck_deliveries_attempts",
        ),
        sa.CheckConstraint(
            "status_code IS NULL OR status_code BETWEEN 100 AND 599",
            name="ck_deliveries_status_code",
        ),
    )
    op.create_index(
        "ix_notification_deliveries_incident_id",
        "notification_deliveries",
        ["incident_id"],
    )

    op.create_table(
        "scheduler_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("running", sa.Boolean(), nullable=False),
        sa.Column("cycle_in_progress", sa.Boolean(), nullable=False),
        sa.Column("poll_seconds", sa.Float(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_cycle_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_cycle_finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("checks_executed", sa.Integer(), nullable=False),
        sa.Column(
            "retention_last_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_scheduler_singleton"),
        sa.CheckConstraint("poll_seconds > 0", name="ck_scheduler_poll"),
        sa.CheckConstraint("checks_executed >= 0", name="ck_scheduler_checks"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.String(length=80), nullable=True),
        sa.Column("actor", sa.String(length=80), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
    )
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("scheduler_state")
    op.drop_table("notification_deliveries")
    op.drop_table("incidents")
    op.drop_table("check_results")
    op.drop_table("services")
