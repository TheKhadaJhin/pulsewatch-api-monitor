"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings with safe defaults for local development."""

    database_path: Path = Path("data/pulsewatch.db")
    check_concurrency: int = 5
    retry_backoff_seconds: float = 0.2
    degraded_latency_ms: float = 750.0
    allow_private_hosts: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_path=Path(
                os.getenv("PULSEWATCH_DATABASE_PATH", "data/pulsewatch.db")
            ),
            check_concurrency=_as_int(
                "PULSEWATCH_CHECK_CONCURRENCY", default=5, minimum=1, maximum=20
            ),
            retry_backoff_seconds=_as_float(
                "PULSEWATCH_RETRY_BACKOFF_SECONDS",
                default=0.2,
                minimum=0.0,
                maximum=10.0,
            ),
            degraded_latency_ms=_as_float(
                "PULSEWATCH_DEGRADED_LATENCY_MS",
                default=750.0,
                minimum=1.0,
                maximum=60_000.0,
            ),
            allow_private_hosts=_as_bool(
                os.getenv("PULSEWATCH_ALLOW_PRIVATE_HOSTS"), default=False
            ),
        )
