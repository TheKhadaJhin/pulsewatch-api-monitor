from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.repository import Repository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "pulsewatch-test.db",
        check_concurrency=3,
        retry_backoff_seconds=0,
        degraded_latency_ms=1_000,
        allow_private_hosts=False,
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
) -> AsyncIterator[TestClient]:
    transport = httpx.MockTransport(default_http_handler)
    application = create_app(settings, monitor_transport=transport)
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture
async def repository(settings: Settings) -> Repository:
    database = Database(settings.database_path)
    await database.initialize()
    return Repository(database)


def create_service(
    client: TestClient,
    *,
    name: str = "Example API",
    url: str = "https://service.test/healthy",
    **overrides,
) -> dict:
    payload = {
        "name": name,
        "url": url,
        "method": "GET",
        "expected_status": 200,
        "timeout_seconds": 1,
        "retry_count": 2,
        **overrides,
    }
    response = client.post("/api/services", json=payload)
    assert response.status_code == 201, response.text
    return response.json()
