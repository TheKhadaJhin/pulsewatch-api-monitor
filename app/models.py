"""Pydantic request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HttpMethod = Literal["GET", "HEAD"]
ServiceState = Literal["healthy", "degraded", "down", "never_checked"]


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


class ServiceCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=80)
    url: str
    method: HttpMethod = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)
    timeout_seconds: float = Field(default=5.0, gt=0.1, le=30.0)
    retry_count: int = Field(default=2, ge=0, le=3)
    active: bool = True

    _url_validator = field_validator("url")(_validate_http_url)


class ServiceUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=80)
    url: str | None = None
    method: HttpMethod | None = None
    expected_status: int | None = Field(default=None, ge=100, le=599)
    timeout_seconds: float | None = Field(default=None, gt=0.1, le=30.0)
    retry_count: int | None = Field(default=None, ge=0, le=3)
    active: bool | None = None

    @field_validator("url")
    @classmethod
    def validate_optional_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value)

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> ServiceUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class ServiceOut(BaseModel):
    id: int
    name: str
    url: str
    method: HttpMethod
    expected_status: int
    timeout_seconds: float
    retry_count: int
    active: bool
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
    error_type: str | None
    error_message: str | None


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
    uptime_percentage: float | None
    average_latency_ms: float | None
    services: list[ServiceMetric]


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str
    database: Literal["connected"]
