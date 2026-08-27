from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import RunChecksRequest, ServiceCreate, ServiceUpdate


def test_settings_are_loaded_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSEWATCH_DATABASE_PATH", "custom/data.db")
    monkeypatch.setenv("PULSEWATCH_CHECK_CONCURRENCY", "7")
    monkeypatch.setenv("PULSEWATCH_RETRY_BACKOFF_SECONDS", "0.5")
    monkeypatch.setenv("PULSEWATCH_DEGRADED_LATENCY_MS", "900")
    monkeypatch.setenv("PULSEWATCH_ALLOW_PRIVATE_HOSTS", "yes")

    settings = Settings.from_env()
    assert settings.database_path == Path("custom/data.db")
    assert settings.check_concurrency == 7
    assert settings.retry_backoff_seconds == 0.5
    assert settings.degraded_latency_ms == 900
    assert settings.allow_private_hosts is True


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("PULSEWATCH_CHECK_CONCURRENCY", "zero", "must be an integer"),
        ("PULSEWATCH_CHECK_CONCURRENCY", "25", "must be between"),
        ("PULSEWATCH_RETRY_BACKOFF_SECONDS", "fast", "must be a number"),
        ("PULSEWATCH_DEGRADED_LATENCY_MS", "0", "must be between"),
    ],
)
def test_invalid_environment_values_fail_fast(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        Settings.from_env()


def test_model_normalizes_selected_ids() -> None:
    request = RunChecksRequest(service_ids=[3, 3, 2])
    assert request.service_ids == [3, 2]


def test_service_model_strips_names_and_validates_bounds() -> None:
    service = ServiceCreate(name="  Search API  ", url="https://example.com")
    assert service.name == "Search API"

    with pytest.raises(ValidationError):
        ServiceCreate(name="X", url="https://example.com")
    with pytest.raises(ValidationError):
        ServiceCreate(name="Valid", url="https://example.com", retry_count=4)


def test_update_requires_a_field() -> None:
    with pytest.raises(ValidationError, match="At least one field"):
        ServiceUpdate()
