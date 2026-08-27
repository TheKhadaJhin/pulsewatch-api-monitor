from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.metrics import AppMetrics
from app.models import IncidentTransition, ServiceCreate
from app.notifications import WebhookNotifier
from app.repository import Repository


async def open_incident(repository: Repository) -> IncidentTransition:
    service = await repository.create_service(
        ServiceCreate(
            name="Webhook service", url="https://webhook-service.example/health"
        )
    )
    recorded = await repository.record_check_result(
        service=service,
        status_code=503,
        latency_ms=25,
        is_healthy=False,
        attempts=3,
        trigger="scheduled",
        error_type="UnexpectedStatus",
        error_message="Expected HTTP 200, received 503",
        request_id=None,
    )
    assert recorded.transition is not None
    return recorded.transition


@pytest.mark.asyncio
async def test_unconfigured_notifier_is_a_noop(
    repository: Repository, settings: Settings
) -> None:
    transition = await open_incident(repository)
    notifier = WebhookNotifier(repository, settings, AppMetrics())
    assert notifier.configured is False
    assert await notifier.notify(transition) is None
    assert await repository.list_notification_deliveries() == []


@pytest.mark.asyncio
async def test_signed_webhook_payload_and_delivery_audit(
    repository: Repository, settings: Settings
) -> None:
    transition = await open_incident(repository)
    secret = "super-secret-signing-value"
    configured = replace(
        settings,
        webhook_url="https://hooks.example/events",
        webhook_secret=secret,
    )
    captured: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = request
        return httpx.Response(204)

    metrics = AppMetrics()
    notifier = WebhookNotifier(
        repository,
        configured,
        metrics,
        transport=httpx.MockTransport(handler),
    )
    assert notifier.configured is True
    delivery = await notifier.notify(transition)

    assert delivery is not None
    assert delivery.delivered is True
    assert delivery.status_code == 204
    assert delivery.attempts == 1
    assert captured is not None
    assert captured.headers["x-pulsewatch-event"] == "incident_opened"
    assert captured.headers["idempotency-key"] == (
        f"pulsewatch:{transition.incident.id}:incident_opened"
    )
    expected = hmac.new(secret.encode(), captured.content, hashlib.sha256).hexdigest()
    assert captured.headers["x-pulsewatch-signature"] == f"sha256={expected}"
    assert b'"event":"incident_opened"' in captured.content
    assert b'"service"' in captured.content
    assert b'"incident"' in captured.content
    assert 'outcome="delivered"' in metrics.render().decode()
    persisted = await repository.list_notification_deliveries()
    assert persisted[0].delivered is True


@pytest.mark.asyncio
async def test_webhook_retries_transient_server_error(
    repository: Repository, settings: Settings
) -> None:
    transition = await open_incident(repository)
    configured = replace(
        settings,
        webhook_url="https://hooks.example/events",
        webhook_retry_count=2,
        retry_backoff_seconds=0,
    )
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls < 3 else 202)

    notifier = WebhookNotifier(
        repository,
        configured,
        AppMetrics(),
        transport=httpx.MockTransport(handler),
    )
    delivery = await notifier.notify(transition)
    assert delivery is not None and delivery.delivered is True
    assert delivery.attempts == 3
    assert calls == 3


@pytest.mark.asyncio
async def test_webhook_does_not_retry_client_error(
    repository: Repository, settings: Settings
) -> None:
    transition = await open_incident(repository)
    configured = replace(
        settings,
        webhook_url="https://hooks.example/events",
        webhook_retry_count=2,
        retry_backoff_seconds=0,
    )
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400)

    notifier = WebhookNotifier(
        repository,
        configured,
        AppMetrics(),
        transport=httpx.MockTransport(handler),
    )
    delivery = await notifier.notify(transition)
    assert delivery is not None
    assert delivery.delivered is False
    assert delivery.status_code == 400
    assert delivery.attempts == 1
    assert delivery.error_message == "Webhook returned HTTP 400"
    assert calls == 1


@pytest.mark.asyncio
async def test_webhook_timeout_then_success(
    repository: Repository, settings: Settings
) -> None:
    transition = await open_incident(repository)
    configured = replace(
        settings,
        webhook_url="https://hooks.example/events",
        webhook_retry_count=1,
        retry_backoff_seconds=0,
    )
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200)

    notifier = WebhookNotifier(
        repository,
        configured,
        AppMetrics(),
        transport=httpx.MockTransport(handler),
    )
    delivery = await notifier.notify(transition)
    assert delivery is not None and delivery.delivered is True
    assert delivery.attempts == 2


@pytest.mark.asyncio
async def test_webhook_network_error_is_normalized(
    repository: Repository, settings: Settings
) -> None:
    transition = await open_incident(repository)
    configured = replace(
        settings,
        webhook_url="https://hooks.example/events",
        webhook_retry_count=0,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    notifier = WebhookNotifier(
        repository,
        configured,
        AppMetrics(),
        transport=httpx.MockTransport(handler),
    )
    delivery = await notifier.notify(transition)
    assert delivery is not None
    assert delivery.delivered is False
    assert delivery.status_code is None
    assert delivery.error_message == "connection refused"


@pytest.mark.asyncio
async def test_private_webhook_target_is_blocked_without_network_call(
    repository: Repository, settings: Settings
) -> None:
    transition = await open_incident(repository)
    configured = replace(settings, webhook_url="http://127.0.0.1:9999/events")
    called = False

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    metrics = AppMetrics()
    notifier = WebhookNotifier(
        repository,
        configured,
        metrics,
        transport=httpx.MockTransport(handler),
    )
    delivery = await notifier.notify(transition)
    assert delivery is not None and delivery.delivered is False
    assert delivery.attempts == 1
    assert "disabled" in (delivery.error_message or "")
    assert called is False
    assert 'outcome="blocked"' in metrics.render().decode()
