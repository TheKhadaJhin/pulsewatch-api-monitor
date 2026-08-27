from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.metrics import AppMetrics
from app.models import ServiceCreate
from app.monitoring import ApiMonitor, ServiceSelectionError, validate_target
from app.notifications import WebhookNotifier
from app.repository import Repository


def build_monitor(
    repository: Repository,
    settings: Settings,
    handler: httpx.AsyncBaseTransport,
) -> ApiMonitor:
    metrics = AppMetrics()
    notifier = WebhookNotifier(repository, settings, metrics)
    return ApiMonitor(
        repository,
        settings,
        notifier,
        metrics,
        transport=handler,
    )


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

    monitor = build_monitor(repository, settings, httpx.MockTransport(handler))
    result = (
        await monitor.run_checks(
            service_ids=[service.id],
            trigger="scheduled",
            request_id="scheduler-cycle-1",
        )
    )[0]

    assert calls == 2
    assert result.is_healthy is True
    assert result.attempts == 2
    assert result.error_type is None
    assert result.trigger == "scheduled"
    assert result.request_id == "scheduler-cycle-1"
    assert await repository.list_incidents() == []


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

    monitor = build_monitor(repository, settings, httpx.MockTransport(handler))
    results = await monitor.run_checks(concurrency=2)

    assert len(results) == 6
    assert all(result.is_healthy for result in results)
    assert maximum_active == 2


@pytest.mark.asyncio
async def test_batch_lock_serializes_overlapping_batches(
    repository: Repository, settings: Settings
) -> None:
    await repository.create_service(
        ServiceCreate(
            name="Serialized",
            url="https://serialized.example/health",
            retry_count=0,
        )
    )
    active_batches = 0
    maximum_active = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal active_batches, maximum_active
        active_batches += 1
        maximum_active = max(maximum_active, active_batches)
        await asyncio.sleep(0.02)
        active_batches -= 1
        return httpx.Response(200)

    monitor = build_monitor(repository, settings, httpx.MockTransport(handler))
    first, second = await asyncio.gather(
        monitor.run_checks(),
        monitor.run_checks(),
    )
    assert len(first) == len(second) == 1
    assert maximum_active == 1


@pytest.mark.asyncio
async def test_request_error_is_normalized_and_opens_incident(
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

    monitor = build_monitor(repository, settings, httpx.MockTransport(handler))
    result = (await monitor.run_checks(service_ids=[service.id]))[0]

    assert result.is_healthy is False
    assert result.error_type == "ConnectError"
    assert result.error_message == "connection refused"
    incidents = await repository.list_incidents(status="open")
    assert incidents[0].initial_error_type == "ConnectError"


@pytest.mark.asyncio
async def test_empty_and_invalid_selections(
    repository: Repository, settings: Settings
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    monitor = build_monitor(repository, settings, httpx.MockTransport(handler))
    assert await monitor.run_checks() == []
    with pytest.raises(ServiceSelectionError) as error:
        await monitor.run_checks(service_ids=[9, 8])
    assert error.value.service_ids == [9, 8]


@pytest.mark.asyncio
async def test_monitor_sends_expected_headers(
    repository: Repository, settings: Settings
) -> None:
    service = await repository.create_service(
        ServiceCreate(
            name="Header check",
            url="https://headers.example/health",
            method="HEAD",
            retry_count=0,
        )
    )
    captured: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = request
        return httpx.Response(200)

    monitor = build_monitor(repository, settings, httpx.MockTransport(handler))
    await monitor.run_checks(service_ids=[service.id])
    assert captured is not None
    assert captured.method == "HEAD"
    assert captured.headers["user-agent"].startswith("PulseWatch/2.0")
    assert captured.headers["accept"] == "*/*"


@pytest.mark.asyncio
async def test_redirects_are_recorded_without_following_private_location(
    repository: Repository, settings: Settings
) -> None:
    service = await repository.create_service(
        ServiceCreate(
            name="Redirect safety",
            url="https://redirect.example/health",
            retry_count=0,
        )
    )
    requested_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "http://127.0.0.1/private"},
        )

    monitor = build_monitor(repository, settings, httpx.MockTransport(handler))
    result = (await monitor.run_checks(service_ids=[service.id]))[0]
    assert requested_urls == ["https://redirect.example/health"]
    assert result.is_healthy is False
    assert result.status_code == 302
    assert result.error_type == "UnexpectedStatus"


@pytest.mark.asyncio
async def test_check_does_not_download_response_body(
    repository: Repository, settings: Settings
) -> None:
    service = await repository.create_service(
        ServiceCreate(
            name="Large response",
            url="https://large.example/health",
            retry_count=0,
        )
    )

    class BodyMustNotBeRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("monitor must not consume target bodies")
            yield b""  # pragma: no cover

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BodyMustNotBeRead())

    monitor = build_monitor(repository, settings, httpx.MockTransport(handler))
    result = (await monitor.run_checks(service_ids=[service.id]))[0]
    assert result.is_healthy is True


@pytest.mark.asyncio
async def test_implicit_environment_proxy_is_ignored(
    repository: Repository,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await repository.create_service(
        ServiceCreate(
            name="Proxy safety",
            url="http://127.0.0.1/private",
            retry_count=0,
        )
    )
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9999")
    metrics = AppMetrics()
    notifier = WebhookNotifier(repository, settings, metrics)
    monitor = ApiMonitor(repository, settings, notifier, metrics)
    result = (await monitor.run_checks(service_ids=[service.id]))[0]
    assert result.error_type == "BlockedTarget"


@pytest.mark.asyncio
async def test_service_deleted_during_check_becomes_selection_error(
    repository: Repository, settings: Settings
) -> None:
    service = await repository.create_service(
        ServiceCreate(
            name="Concurrent deletion",
            url="https://deleted.example/health",
            retry_count=0,
        )
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    monitor = build_monitor(repository, settings, httpx.MockTransport(handler))
    repository.record_check_result = AsyncMock(
        side_effect=LookupError("service deleted")
    )
    with pytest.raises(ServiceSelectionError) as error:
        await monitor.run_checks(service_ids=[service.id])
    assert error.value.service_ids == [service.id]


def test_target_validation_allows_public_and_configured_private_hosts() -> None:
    validate_target("https://api.example.com/health", allow_private_hosts=False)
    validate_target("http://127.0.0.1:8000/health", allow_private_hosts=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/health",
        "http://api.localhost/health",
        "http://service.local/health",
        "http://service.internal/health",
        "http://10.1.2.3/health",
        "http://127.0.0.1/health",
        "http://169.254.169.254/metadata",
        "http://[::1]/health",
        "http://192.0.2.1/health",
    ],
)
def test_target_validation_blocks_private_destinations(url: str) -> None:
    with pytest.raises(ValueError):
        validate_target(url, allow_private_hosts=False)


def test_target_validation_requires_hostname() -> None:
    with pytest.raises(ValueError, match="no hostname"):
        validate_target("https:///missing", allow_private_hosts=False)
