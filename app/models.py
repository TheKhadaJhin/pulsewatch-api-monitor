"""Pydantic request, response, and domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HttpMethod = Literal["GET", "HEAD"]
ServiceState = Literal["healthy", "degraded", "down", "never_checked"]
CheckTrigger = Literal["manual", "scheduled"]
IncidentStatus = Literal["open", "resolved"]
IncidentEventType = Literal["incident_opened", "incident_resolved"]


def _validate_http_url(value: str) -> str:
    value = value.strip()
    if len(value) > 2_048:
        raise ValueError("URL must be 2,048 characters or fewer")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL must use http or https")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")
    return value


def _normalize_tags(value: list[str]) -> list[str]:
    normalized = []
    for raw_tag in value:
        tag = raw_tag.strip().lower()
        if not tag:
            continue
        if len(tag) > 32:
            raise ValueError("Tags must be 32 characters or fewer")
        if tag not in normalized:
            normalized.append(tag)
    if len(normalized) > 10:
        raise ValueError("A service can have at most 10 tags")
    return normalized


class ServiceCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=80)
    description: str | None = Field(default=None, max_length=240)
    url: str
    method: HttpMethod = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)
    timeout_seconds: float = Field(default=5.0, gt=0.1, le=30.0)
    retry_count: int = Field(default=2, ge=0, le=3)
    check_interval_seconds: int = Field(default=300, ge=30, le=86_400)
    active: bool = True
    tags: list[str] = Field(default_factory=list)

    _url_validator = field_validator("url")(_validate_http_url)
    _tags_validator = field_validator("tags")(_normalize_tags)


class ServiceUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=80)
    description: str | None = Field(default=None, max_length=240)
    url: str | None = None
    method: HttpMethod | None = None
    expected_status: int | None = Field(default=None, ge=100, le=599)
    timeout_seconds: float | None = Field(default=None, gt=0.1, le=30.0)
    retry_count: int | None = Field(default=None, ge=0, le=3)
    check_interval_seconds: int | None = Field(default=None, ge=30, le=86_400)
    active: bool | None = None
    tags: list[str] | None = None

    @field_validator("url")
    @classmethod
    def validate_optional_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value)

    @field_validator("tags")
    @classmethod
    def validate_optional_tags(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _normalize_tags(value)

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> ServiceUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class ServiceOut(BaseModel):
    id: int
    name: str
    description: str | None
    url: str
    method: HttpMethod
    expected_status: int
    timeout_seconds: float
    retry_count: int
    check_interval_seconds: int
    active: bool
    tags: list[str]
    next_check_at: datetime
    created_at: datetime
    updated_at: datetime


class CheckResultOut(BaseModel):
    id: int
    service_id: int
    checked_at: datetime
    status_code: int | None
    latency_ms: float | None
    is_healthy: bool
    attempts: int
    trigger: CheckTrigger
    error_type: str | None
    error_message: str | None
    request_id: str | None


class RunChecksRequest(BaseModel):
    service_ids: list[int] | None = Field(default=None, max_length=100)
    concurrency: int | None = Field(default=None, ge=1, le=20)

    @field_validator("service_ids")
    @classmethod
    def unique_positive_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(item <= 0 for item in value):
            raise ValueError("Service IDs must be positive integers")
        return list(dict.fromkeys(value))


class RunChecksResponse(BaseModel):
    trigger: CheckTrigger
    total: int
    healthy: int
    failed: int
    results: list[CheckResultOut]


class ServiceMetric(BaseModel):
    id: int
    name: str
    url: str
    state: ServiceState
    last_checked_at: datetime | None
    last_status_code: int | None
    last_latency_ms: float | None
    next_check_at: datetime
    checks: int
    uptime_percentage: float | None


class SummaryOut(BaseModel):
    generated_at: datetime
    window_hours: int
    services_total: int
    healthy_services: int
    degraded_services: int
    down_services: int
    never_checked: int
    checks_total: int
    incidents_total: int
    open_incidents: int
    uptime_percentage: float | None
    average_latency_ms: float | None
    services: list[ServiceMetric]


class IncidentOut(BaseModel):
    id: int
    service_id: int
    service_name: str
    status: IncidentStatus
    opened_at: datetime
    resolved_at: datetime | None
    last_failure_at: datetime
    duration_seconds: float | None
    failure_count: int
    initial_error_type: str | None
    initial_error_message: str | None
    last_error_type: str | None
    last_error_message: str | None


class IncidentTransition(BaseModel):
    event_type: IncidentEventType
    incident: IncidentOut
    service: ServiceOut


class NotificationDeliveryOut(BaseModel):
    id: int
    incident_id: int
    event_type: IncidentEventType
    attempted_at: datetime
    delivered: bool
    status_code: int | None
    attempts: int
    error_message: str | None


class AuditEventOut(BaseModel):
    id: int
    occurred_at: datetime
    action: str
    entity_type: str
    entity_id: str | None
    actor: str
    request_id: str | None
    metadata: dict[str, Any]


class SchedulerStatusOut(BaseModel):
    enabled: bool
    running: bool
    cycle_in_progress: bool
    poll_seconds: float
    heartbeat_at: datetime | None
    last_cycle_started_at: datetime | None
    last_cycle_finished_at: datetime | None
    last_error: str | None
    checks_executed: int
    retention_last_run_at: datetime | None


class SchedulerRunOut(BaseModel):
    checks_executed: int
    status: SchedulerStatusOut


class PublicConfigOut(BaseModel):
    version: str
    environment: str
    authentication_required: bool
    scheduler_enabled: bool
    webhook_configured: bool
    retention_days: int
    metrics_enabled: bool


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str


class ReadinessOut(HealthOut):
    database: Literal["connected"]
    scheduler: Literal["enabled", "disabled"]


class RetentionResultOut(BaseModel):
    deleted_check_results: int
    cutoff: datetime
