"""Typed application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("Boolean values must be true/false, yes/no, on/off, or 1/0")


def _as_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _as_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _database_url() -> str:
    configured = os.getenv("PULSEWATCH_DATABASE_URL")
    if configured:
        return configured
    legacy_path = os.getenv("PULSEWATCH_DATABASE_PATH", "data/pulsewatch.db")
    return f"sqlite+aiosqlite:///{legacy_path}"


def database_url_from_env() -> str:
    value = _database_url()
    supported = ("sqlite+aiosqlite://", "postgresql+asyncpg://")
    if not value.startswith(supported):
        raise ValueError(
            "PULSEWATCH_DATABASE_URL must use sqlite+aiosqlite or postgresql+asyncpg"
        )
    return value


def _optional_http_url(name: str) -> str | None:
    value = os.getenv(name)
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be a valid HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{name} must not contain credentials")
    return value


def _cors_origins() -> tuple[str, ...]:
    origins = []
    for raw_origin in os.getenv("PULSEWATCH_CORS_ORIGINS", "").split(","):
        origin = raw_origin.strip().rstrip("/")
        if not origin:
            continue
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "PULSEWATCH_CORS_ORIGINS entries must be HTTP(S) origins "
                "without credentials, paths, queries, or fragments"
            )
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings with development-safe and production-capable defaults."""

    database_url: str = "sqlite+aiosqlite:///data/pulsewatch.db"
    environment: str = "development"
    auto_create_schema: bool = True
    check_concurrency: int = 5
    retry_backoff_seconds: float = 0.2
    degraded_latency_ms: float = 750.0
    allow_private_hosts: bool = False
    scheduler_enabled: bool = True
    scheduler_poll_seconds: float = 5.0
    scheduler_batch_size: int = 100
    retention_days: int = 30
    retention_interval_seconds: int = 3_600
    admin_api_key: str | None = None
    webhook_url: str | None = None
    webhook_secret: str | None = None
    webhook_timeout_seconds: float = 5.0
    webhook_retry_count: int = 2
    metrics_enabled: bool = True
    log_level: str = "INFO"
    log_json: bool = False
    cors_origins: tuple[str, ...] = ()

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite+")

    @classmethod
    def for_sqlite(cls, path: Path, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "database_url": f"sqlite+aiosqlite:///{path}",
            "scheduler_enabled": False,
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    @classmethod
    def from_env(cls) -> Settings:
        environment = os.getenv("PULSEWATCH_ENVIRONMENT", "development").strip()
        log_level = os.getenv("PULSEWATCH_LOG_LEVEL", "INFO").upper().strip()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("PULSEWATCH_LOG_LEVEL is invalid")

        origins = _cors_origins()
        api_key = os.getenv("PULSEWATCH_ADMIN_API_KEY") or None
        if api_key and len(api_key) < 16:
            raise ValueError("PULSEWATCH_ADMIN_API_KEY must be at least 16 characters")
        webhook_url = _optional_http_url("PULSEWATCH_WEBHOOK_URL")
        webhook_secret = os.getenv("PULSEWATCH_WEBHOOK_SECRET") or None
        if webhook_secret and len(webhook_secret) < 16:
            raise ValueError("PULSEWATCH_WEBHOOK_SECRET must be at least 16 characters")
        if environment.lower() == "production":
            if api_key is None:
                raise ValueError("PULSEWATCH_ADMIN_API_KEY is required in production")
            if webhook_url and webhook_secret is None:
                raise ValueError(
                    "PULSEWATCH_WEBHOOK_SECRET is required for production webhooks"
                )

        return cls(
            database_url=database_url_from_env(),
            environment=environment,
            auto_create_schema=_as_bool(
                os.getenv("PULSEWATCH_AUTO_CREATE_SCHEMA"),
                default=environment.lower() != "production",
            ),
            check_concurrency=_as_int(
                "PULSEWATCH_CHECK_CONCURRENCY", default=5, minimum=1, maximum=20
            ),
            retry_backoff_seconds=_as_float(
                "PULSEWATCH_RETRY_BACKOFF_SECONDS", 0.2, 0.0, 10.0
            ),
            degraded_latency_ms=_as_float(
                "PULSEWATCH_DEGRADED_LATENCY_MS", 750.0, 1.0, 60_000.0
            ),
            allow_private_hosts=_as_bool(
                os.getenv("PULSEWATCH_ALLOW_PRIVATE_HOSTS"), default=False
            ),
            scheduler_enabled=_as_bool(
                os.getenv("PULSEWATCH_SCHEDULER_ENABLED"), default=True
            ),
            scheduler_poll_seconds=_as_float(
                "PULSEWATCH_SCHEDULER_POLL_SECONDS", 5.0, 0.1, 3_600.0
            ),
            scheduler_batch_size=_as_int(
                "PULSEWATCH_SCHEDULER_BATCH_SIZE", 100, 1, 1_000
            ),
            retention_days=_as_int("PULSEWATCH_RETENTION_DAYS", 30, 1, 3_650),
            retention_interval_seconds=_as_int(
                "PULSEWATCH_RETENTION_INTERVAL_SECONDS", 3_600, 60, 604_800
            ),
            admin_api_key=api_key,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            webhook_timeout_seconds=_as_float(
                "PULSEWATCH_WEBHOOK_TIMEOUT_SECONDS", 5.0, 0.2, 30.0
            ),
            webhook_retry_count=_as_int("PULSEWATCH_WEBHOOK_RETRY_COUNT", 2, 0, 5),
            metrics_enabled=_as_bool(
                os.getenv("PULSEWATCH_METRICS_ENABLED"), default=True
            ),
            log_level=log_level,
            log_json=_as_bool(os.getenv("PULSEWATCH_LOG_JSON"), default=False),
            cors_origins=origins,
        )
