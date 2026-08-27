from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.models import ServiceCreate
from app.monitoring import ApiMonitor, validate_target
from app.repository import Repository


@pytest.mark.asyncio
async def test_flaky_server_recovers_on_second_attempt(
    repository: Repository, settings: Settings
) -> None:
    service = await repository.create_service(
        ServiceCreate(
            name="Flaky",
            url="https://flaky.example/health",
            retry_count=2,
        )
    )
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200)

    monitor = ApiMonitor(repository, settings, transport=httpx.MockTransport(handler))
    result = (await monitor.run_checks(service_ids=[service.id]))[0]

    assert calls == 2
    assert result.is_healthy is True
    assert result.attempts == 2
    assert result.error_type is None


@pytest.mark.asyncio
async def test_concurrency_limit_is_respected(
    repository: Repository, settings: Settings
) -> None:
    for index in range(6):
        await repository.create_service(
            ServiceCreate(
                name=f"Service {index}",
                url=f"https://service-{index}.example/health",
                retry_count=0,
            )
        )

    active_requests = 0
    maximum_active = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal active_requests, maximum_active
        active_requests += 1
        maximum_active = max(maximum_active, active_requests)
        await asyncio.sleep(0.015)
        active_requests -= 1
        return httpx.Response(200)

    monitor = ApiMonitor(repository, settings, transport=httpx.MockTransport(handler))
    results = await monitor.run_checks(concurrency=2)

    assert len(results) == 6
    assert all(result.is_healthy for result in results)
    assert maximum_active == 2


@pytest.mark.asyncio
async def test_request_error_is_normalized(
    repository: Repository, settings: Settings
) -> None:
    service = await repository.create_service(
        ServiceCreate(
            name="Broken DNS",
            url="https://broken.example/health",
            retry_count=0,
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monitor = ApiMonitor(repository, settings, transport=httpx.MockTransport(handler))
    result = (await monitor.run_checks(service_ids=[service.id]))[0]

    assert result.is_healthy is False
    assert result.error_type == "ConnectError"
    assert result.error_message == "connection refused"


def test_target_validation_allows_public_and_configured_private_hosts() -> None:
    validate_target("https://api.example.com/health", allow_private_hosts=False)
    validate_target("http://127.0.0.1:8000/health", allow_private_hosts=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/health",
        "http://service.local/health",
        "http://10.1.2.3/health",
        "http://127.0.0.1/health",
        "http://[::1]/health",
    ],
)
def test_target_validation_blocks_private_destinations(url: str) -> None:
    with pytest.raises(ValueError):
        validate_target(url, allow_private_hosts=False)
