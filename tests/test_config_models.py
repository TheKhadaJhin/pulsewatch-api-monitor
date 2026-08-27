from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import RunChecksRequest, ServiceCreate, ServiceUpdate


def test_settings_are_loaded_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSEWATCH_DATABASE_URL", "sqlite+aiosqlite:///custom/data.db")
    monkeypatch.setenv("PULSEWATCH_ENVIRONMENT", "staging")
    monkeypatch.setenv("PULSEWATCH_CHECK_CONCURRENCY", "7")
    monkeypatch.setenv("PULSEWATCH_RETRY_BACKOFF_SECONDS", "0.5")
    monkeypatch.setenv("PULSEWATCH_DEGRADED_LATENCY_MS", "900")
    monkeypatch.setenv("PULSEWATCH_ALLOW_PRIVATE_HOSTS", "yes")
    monkeypatch.setenv("PULSEWATCH_SCHEDULER_ENABLED", "no")
    monkeypatch.setenv("PULSEWATCH_RETENTION_DAYS", "60")
    monkeypatch.setenv("PULSEWATCH_ADMIN_API_KEY", "a" * 24)
    monkeypatch.setenv("PULSEWATCH_WEBHOOK_URL", "https://hooks.example.com/events")
    monkeypatch.setenv("PULSEWATCH_CORS_ORIGINS", "https://one.test, https://two.test")
    monkeypatch.setenv("PULSEWATCH_LOG_JSON", "true")

    settings = Settings.from_env()
    assert settings.database_url == "sqlite+aiosqlite:///custom/data.db"
    assert settings.environment == "staging"
    assert settings.check_concurrency == 7
    assert settings.retry_backoff_seconds == 0.5
    assert settings.degraded_latency_ms == 900
    assert settings.allow_private_hosts is True
    assert settings.scheduler_enabled is False
    assert settings.retention_days == 60
    assert settings.admin_api_key == "a" * 24
    assert settings.webhook_url == "https://hooks.example.com/events"
    assert settings.cors_origins == ("https://one.test", "https://two.test")
    assert settings.log_json is True


def test_legacy_database_path_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULSEWATCH_DATABASE_URL", raising=False)
    monkeypatch.setenv("PULSEWATCH_DATABASE_PATH", "legacy/pulsewatch.db")
    assert (
        Settings.from_env().database_url == "sqlite+aiosqlite:///legacy/pulsewatch.db"
    )


def test_sqlite_test_factory(tmp_path: Path) -> None:
    settings = Settings.for_sqlite(tmp_path / "test.db", check_concurrency=2)
    assert settings.database_url.endswith("/test.db")
    assert settings.scheduler_enabled is False
    assert settings.check_concurrency == 2
    assert settings.is_sqlite is True


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("PULSEWATCH_CHECK_CONCURRENCY", "zero", "must be an integer"),
        ("PULSEWATCH_CHECK_CONCURRENCY", "25", "must be between"),
        ("PULSEWATCH_RETRY_BACKOFF_SECONDS", "fast", "must be a number"),
        ("PULSEWATCH_DEGRADED_LATENCY_MS", "0", "must be between"),
        ("PULSEWATCH_SCHEDULER_ENABLED", "sometimes", "Boolean values"),
        ("PULSEWATCH_LOG_LEVEL", "VERBOSE", "LOG_LEVEL is invalid"),
    ],
)
def test_invalid_environment_values_fail_fast(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        Settings.from_env()


def test_short_admin_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSEWATCH_ADMIN_API_KEY", "short")
    with pytest.raises(ValueError, match="at least 16"):
        Settings.from_env()


def test_production_requires_auth_and_disables_auto_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PULSEWATCH_ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="required in production"):
        Settings.from_env()

    monkeypatch.setenv("PULSEWATCH_ADMIN_API_KEY", "a" * 24)
    settings = Settings.from_env()
    assert settings.auto_create_schema is False


def test_production_webhook_requires_signing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PULSEWATCH_ENVIRONMENT", "production")
    monkeypatch.setenv("PULSEWATCH_ADMIN_API_KEY", "a" * 24)
    monkeypatch.setenv("PULSEWATCH_WEBHOOK_URL", "https://hooks.example.com/events")
    with pytest.raises(ValueError, match="WEBHOOK_SECRET is required"):
        Settings.from_env()


def test_short_webhook_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSEWATCH_WEBHOOK_SECRET", "short")
    with pytest.raises(ValueError, match="at least 16"):
        Settings.from_env()


def test_invalid_webhook_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSEWATCH_WEBHOOK_URL", "ftp://example.com")
    with pytest.raises(ValueError, match="HTTP"):
        Settings.from_env()


def test_webhook_url_credentials_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PULSEWATCH_WEBHOOK_URL", "https://user:secret@hooks.example.com/events"
    )
    with pytest.raises(ValueError, match="must not contain credentials"):
        Settings.from_env()


def test_database_driver_must_be_async(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSEWATCH_DATABASE_URL", "postgresql://db.example/pulsewatch")
    with pytest.raises(ValueError, match=r"postgresql\+asyncpg"):
        Settings.from_env()


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://frontend.example",
        "https://user:secret@frontend.example",
        "https://frontend.example/path",
        "https://frontend.example?debug=true",
    ],
)
def test_cors_origins_must_be_origins(
    monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    monkeypatch.setenv("PULSEWATCH_CORS_ORIGINS", origin)
    with pytest.raises(ValueError, match="must be HTTP"):
        Settings.from_env()


def test_model_normalizes_selected_ids() -> None:
    request = RunChecksRequest(service_ids=[3, 3, 2])
    assert request.service_ids == [3, 2]


def test_service_model_normalizes_name_and_tags() -> None:
    service = ServiceCreate(
        name="  Search API  ",
        url="https://example.com",
        tags=[" Production ", "production", "", "Search"],
    )
    assert service.name == "Search API"
    assert service.tags == ["production", "search"]


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "X", "url": "https://example.com"},
        {"name": "Valid", "url": "https://example.com", "retry_count": 4},
        {
            "name": "Valid",
            "url": "https://example.com",
            "check_interval_seconds": 10,
        },
        {"name": "Valid", "url": "ftp://example.com"},
        {"name": "Valid", "url": "https://user:secret@example.com"},
        {
            "name": "Valid",
            "url": "https://example.com",
            "tags": [f"tag-{index}" for index in range(11)],
        },
    ],
)
def test_invalid_service_models(payload: dict) -> None:
    with pytest.raises(ValidationError):
        ServiceCreate(**payload)


def test_update_requires_a_field() -> None:
    with pytest.raises(ValidationError, match="At least one field"):
        ServiceUpdate()
