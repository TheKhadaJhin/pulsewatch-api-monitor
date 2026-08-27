from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import API_PREFIX, create_app
from app.repository import Repository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.for_sqlite(
        tmp_path / "pulsewatch-test.db",
        auto_create_schema=True,
        check_concurrency=3,
        retry_backoff_seconds=0,
        degraded_latency_ms=1_000,
        allow_private_hosts=False,
        scheduler_enabled=False,
        scheduler_poll_seconds=0.01,
        retention_interval_seconds=60,
    )


@pytest.fixture
def default_http_handler() -> Callable[[httpx.Request], httpx.Response]:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/healthy":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/created":
            return httpx.Response(201)
        if path == "/missing":
            return httpx.Response(404)
        if path == "/failing":
            return httpx.Response(503)
        if path == "/timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        return httpx.Response(418)

    return handler


@pytest.fixture
def client(
    settings: Settings,
    default_http_handler: Callable[[httpx.Request], httpx.Response],
) -> Iterator[TestClient]:
    application = create_app(
        settings,
        monitor_transport=httpx.MockTransport(default_http_handler),
    )
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture
async def repository(settings: Settings) -> AsyncIterator[Repository]:
    database = Database(settings.database_url)
    await database.initialize()
    try:
        yield Repository(database)
    finally:
        await database.dispose()


def create_service(
    client: TestClient,
    *,
    name: str = "Example API",
    url: str = "https://service.test/healthy",
    api_key: str | None = None,
    **overrides,
) -> dict:
    payload = {
        "name": name,
        "description": "Portfolio test service",
        "url": url,
        "method": "GET",
        "expected_status": 200,
        "timeout_seconds": 1,
        "retry_count": 2,
        "check_interval_seconds": 300,
        "tags": ["test"],
        **overrides,
    }
    headers = {"X-API-Key": api_key} if api_key else {}
    response = client.post(f"{API_PREFIX}/services", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()
