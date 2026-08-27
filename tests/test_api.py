from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import API_PREFIX, create_app
from tests.conftest import create_service


def test_health_dashboard_docs_and_observability_headers(client: TestClient) -> None:
    live = client.get(f"{API_PREFIX}/health/live")
    assert live.status_code == 200
    assert live.json() == {"status": "ok", "version": "2.0.0"}

    ready = client.get("/api/health", headers={"X-Request-ID": "portfolio-test"})
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ok",
        "version": "2.0.0",
        "database": "connected",
        "scheduler": "disabled",
    }
    assert ready.headers["x-request-id"] == "portfolio-test"
    assert "app;dur=" in ready.headers["server-timing"]
    assert ready.headers["x-content-type-options"] == "nosniff"
    assert ready.headers["x-frame-options"] == "DENY"
    assert ready.headers["referrer-policy"] == "no-referrer"
    assert ready.headers["cache-control"] == "no-store"
    assert ready.headers["cross-origin-opener-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in ready.headers["content-security-policy"]

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "PulseWatch" in dashboard.text
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "'unsafe-inline'" in docs.headers["content-security-policy"]
    assert "cdn.jsdelivr.net" in docs.headers["content-security-policy"]
    assert "'unsafe-inline'" not in dashboard.headers["content-security-policy"]
    unsafe_request_id = client.get(
        f"{API_PREFIX}/health/live",
        headers={"X-Request-ID": "spaces are not allowed"},
    )
    assert unsafe_request_id.headers["x-request-id"] != "spaces are not allowed"
    assert len(unsafe_request_id.headers["x-request-id"]) == 36
    specification = client.get("/openapi.json").json()
    assert specification["info"]["version"] == "2.0.0"
    assert specification["info"]["contact"]["name"] == "Mario Fuentes"
    security_schemes = specification["components"]["securitySchemes"]
    assert security_schemes["AdminApiKey"]["in"] == "header"
    assert security_schemes["AdminApiKey"]["name"] == "X-API-Key"
    assert security_schemes["AdminBearer"]["scheme"] == "bearer"
    assert specification["paths"][f"{API_PREFIX}/services"]["post"]["security"] == [
        {"AdminApiKey": []},
        {"AdminBearer": []},
    ]


def test_public_config_exposes_capabilities_not_secrets(client: TestClient) -> None:
    response = client.get(f"{API_PREFIX}/config")
    assert response.status_code == 200
    assert response.json() == {
        "version": "2.0.0",
        "environment": "development",
        "authentication_required": False,
        "scheduler_enabled": False,
        "webhook_configured": False,
        "retention_days": 30,
        "metrics_enabled": True,
    }
    assert "api_key" not in response.text.lower()


def test_service_crud_contract_and_audit(client: TestClient) -> None:
    created = create_service(client)
    assert created["id"] == 1
    assert created["active"] is True
    assert created["expected_status"] == 200
    assert created["check_interval_seconds"] == 300
    assert created["tags"] == ["test"]
    assert created["description"] == "Portfolio test service"

    fetched = client.get(f"{API_PREFIX}/services/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["url"] == "https://service.test/healthy"

    updated = client.patch(
        f"{API_PREFIX}/services/{created['id']}",
        json={
            "name": "Renamed API",
            "retry_count": 1,
            "active": False,
            "tags": ["Production", "payments"],
        },
        headers={"X-Request-ID": "update-123"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed API"
    assert updated.json()["retry_count"] == 1
    assert updated.json()["active"] is False
    assert updated.json()["tags"] == ["production", "payments"]

    assert len(client.get(f"{API_PREFIX}/services").json()) == 1
    assert client.get(f"{API_PREFIX}/services?active_only=true").json() == []

    audit = client.get(f"{API_PREFIX}/operations/audit").json()
    assert [event["action"] for event in audit[:2]] == [
        "service.updated",
        "service.created",
    ]
    assert audit[0]["actor"] == "local-admin"
    assert audit[0]["request_id"] == "update-123"
    assert "changed_fields" in audit[0]["metadata"]

    deleted = client.delete(f"{API_PREFIX}/services/{created['id']}")
    assert deleted.status_code == 204
    assert client.get(f"{API_PREFIX}/services/{created['id']}").status_code == 404
    assert client.delete(f"{API_PREFIX}/services/{created['id']}").status_code == 404


def test_duplicate_url_returns_conflict(client: TestClient) -> None:
    create_service(client, name="First")
    response = client.post(
        f"{API_PREFIX}/services",
        json={"name": "Duplicate", "url": "https://service.test/healthy"},
    )
    assert response.status_code == 409
    assert "same unique value" in response.json()["detail"]


def test_service_validation_errors_are_explicit(client: TestClient) -> None:
    invalid_url = client.post(
        f"{API_PREFIX}/services",
        json={"name": "Invalid", "url": "ftp://example.com/data"},
    )
    assert invalid_url.status_code == 422
    assert "http or https" in invalid_url.text

    credentials = client.post(
        f"{API_PREFIX}/services",
        json={"name": "Unsafe", "url": "https://user:secret@example.com"},
    )
    assert credentials.status_code == 422
    assert "credentials" in credentials.text

    empty_patch = client.patch(f"{API_PREFIX}/services/999", json={})
    assert empty_patch.status_code == 422
    assert client.get(f"{API_PREFIX}/services?limit=0").status_code == 422
    assert client.get(f"{API_PREFIX}/services?offset=-1").status_code == 422


def test_missing_service_routes_return_404(client: TestClient) -> None:
    assert client.get(f"{API_PREFIX}/services/99").status_code == 404
    response = client.patch(f"{API_PREFIX}/services/99", json={"active": False})
    assert response.status_code == 404


def test_run_checks_records_success_failure_and_incident(client: TestClient) -> None:
    healthy = create_service(client, name="Healthy")
    failing = create_service(client, name="Failing", url="https://service.test/failing")

    response = client.post(
        f"{API_PREFIX}/checks/run",
        json={},
        headers={"X-Request-ID": "manual-batch-1"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["trigger"] == "manual"
    assert payload["total"] == 2
    assert payload["healthy"] == 1
    assert payload["failed"] == 1

    results = {item["service_id"]: item for item in payload["results"]}
    assert results[healthy["id"]]["is_healthy"] is True
    assert results[healthy["id"]]["attempts"] == 1
    assert results[healthy["id"]]["request_id"] == "manual-batch-1"
    assert results[failing["id"]]["is_healthy"] is False
    assert results[failing["id"]]["status_code"] == 503
    assert results[failing["id"]]["attempts"] == 3
    assert results[failing["id"]]["error_type"] == "UnexpectedStatus"

    incidents = client.get(f"{API_PREFIX}/incidents?status=open").json()
    assert len(incidents) == 1
    assert incidents[0]["service_id"] == failing["id"]
    assert incidents[0]["failure_count"] == 1
    assert client.get(f"{API_PREFIX}/operations/notifications").json() == []


def test_incident_is_updated_then_resolved(settings: Settings) -> None:
    status = 503

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    application = create_app(settings, monitor_transport=httpx.MockTransport(handler))
    with TestClient(application) as test_client:
        service = create_service(
            test_client,
            url="https://stateful.test/health",
            retry_count=0,
        )
        test_client.post(f"{API_PREFIX}/checks/run", json={})
        test_client.post(f"{API_PREFIX}/checks/run", json={})
        opened = test_client.get(f"{API_PREFIX}/incidents?status=open").json()[0]
        assert opened["failure_count"] == 2

        status = 200
        recovered = test_client.post(f"{API_PREFIX}/checks/run", json={}).json()
        assert recovered["healthy"] == 1
        resolved = test_client.get(
            f"{API_PREFIX}/incidents?status=resolved&service_id={service['id']}"
        ).json()[0]
        assert resolved["resolved_at"] is not None
        assert resolved["duration_seconds"] is not None
        assert test_client.get(f"{API_PREFIX}/incidents?status=open").json() == []


def test_non_retryable_expected_status_timeout_and_blocked_target(
    client: TestClient,
) -> None:
    missing = create_service(
        client, name="Missing route", url="https://service.test/missing"
    )
    created = create_service(
        client,
        name="Create endpoint",
        url="https://service.test/created",
        expected_status=201,
    )
    timeout = create_service(
        client,
        name="Slow API",
        url="https://service.test/timeout",
        retry_count=1,
    )
    blocked = create_service(
        client,
        name="Metadata",
        url="http://169.254.169.254/latest/meta-data",
    )

    response = client.post(
        f"{API_PREFIX}/checks/run",
        json={
            "service_ids": [
                missing["id"],
                created["id"],
                timeout["id"],
                blocked["id"],
            ]
        },
    )
    results = {item["service_id"]: item for item in response.json()["results"]}
    assert results[missing["id"]]["attempts"] == 1
    assert results[missing["id"]]["error_message"] == (
        "Expected HTTP 200, received 404"
    )
    assert results[created["id"]]["is_healthy"] is True
    assert results[timeout["id"]]["attempts"] == 2
    assert results[timeout["id"]]["error_type"] == "Timeout"
    assert "timeout" in results[timeout["id"]]["error_message"]
    assert results[blocked["id"]]["status_code"] is None
    assert results[blocked["id"]]["error_type"] == "BlockedTarget"


def test_selected_check_rejects_missing_and_inactive_ids(client: TestClient) -> None:
    service = create_service(client)
    missing = client.post(f"{API_PREFIX}/checks/run", json={"service_ids": [99]})
    assert missing.status_code == 404
    assert missing.json()["detail"]["service_ids"] == [99]

    client.patch(f"{API_PREFIX}/services/{service['id']}", json={"active": False})
    inactive = client.post(
        f"{API_PREFIX}/checks/run", json={"service_ids": [service["id"]]}
    )
    assert inactive.status_code == 404


def test_empty_database_check_run_is_valid(client: TestClient) -> None:
    response = client.post(f"{API_PREFIX}/checks/run")
    assert response.status_code == 200
    assert response.json() == {
        "trigger": "manual",
        "total": 0,
        "healthy": 0,
        "failed": 0,
        "results": [],
    }


def test_history_summary_filtering_pagination_and_cascade_delete(
    client: TestClient,
) -> None:
    first = create_service(client, name="First")
    second = create_service(
        client, name="Second", url="https://service-two.test/healthy"
    )
    client.post(f"{API_PREFIX}/checks/run", json={})

    history = client.get(f"{API_PREFIX}/checks?limit=10").json()
    assert len(history) == 2
    assert all(item["trigger"] == "manual" for item in history)
    filtered = client.get(
        f"{API_PREFIX}/checks?service_id={first['id']}&trigger=manual"
    ).json()
    assert len(filtered) == 1
    assert filtered[0]["service_id"] == first["id"]
    assert len(client.get(f"{API_PREFIX}/checks?limit=1&offset=1").json()) == 1

    summary = client.get(f"{API_PREFIX}/summary?window_hours=24").json()
    assert summary["services_total"] == 2
    assert summary["healthy_services"] == 2
    assert summary["checks_total"] == 2
    assert summary["uptime_percentage"] == 100.0
    assert summary["incidents_total"] == 0
    assert summary["open_incidents"] == 0

    client.delete(f"{API_PREFIX}/services/{second['id']}")
    remaining_history = client.get(f"{API_PREFIX}/checks?limit=10").json()
    assert len(remaining_history) == 1
    assert remaining_history[0]["service_id"] == first["id"]


def test_scheduler_retention_metrics_and_query_validation(client: TestClient) -> None:
    service = create_service(client)
    status = client.get(f"{API_PREFIX}/operations/scheduler").json()
    assert status["enabled"] is False
    assert status["running"] is False

    run = client.post(f"{API_PREFIX}/operations/scheduler/run").json()
    assert run["checks_executed"] == 1
    assert run["status"]["checks_executed"] == 1
    checks = client.get(f"{API_PREFIX}/checks?trigger=scheduled").json()
    assert checks[0]["service_id"] == service["id"]

    retention = client.post(f"{API_PREFIX}/operations/retention/run").json()
    assert retention["deleted_check_results"] == 0
    assert "cutoff" in retention

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "pulsewatch_http_requests_total" in metrics.text
    assert 'trigger="scheduled"' in metrics.text

    assert client.get(f"{API_PREFIX}/checks?limit=0").status_code == 422
    assert client.get(f"{API_PREFIX}/checks?limit=501").status_code == 422
    assert client.get(f"{API_PREFIX}/checks?trigger=unknown").status_code == 422
    assert client.get(f"{API_PREFIX}/summary?window_hours=0").status_code == 422
    assert client.get(f"{API_PREFIX}/incidents?status=unknown").status_code == 422
    invalid_ids = client.post(f"{API_PREFIX}/checks/run", json={"service_ids": [-1]})
    assert invalid_ids.status_code == 422


def test_admin_api_key_protects_mutations_and_operations(
    tmp_path: Path,
    default_http_handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    api_key = "portfolio-secret-key-123"
    settings = Settings.for_sqlite(
        tmp_path / "auth.db",
        admin_api_key=api_key,
        scheduler_enabled=False,
        retry_backoff_seconds=0,
    )
    application = create_app(
        settings,
        monitor_transport=httpx.MockTransport(default_http_handler),
    )
    with TestClient(application) as test_client:
        assert test_client.post(f"{API_PREFIX}/services", json={}).status_code == 401
        wrong = test_client.post(
            f"{API_PREFIX}/services", json={}, headers={"X-API-Key": "wrong"}
        )
        assert wrong.status_code == 401
        assert wrong.headers["www-authenticate"] == "Bearer"

        service = create_service(test_client, api_key=api_key)
        assert test_client.get(f"{API_PREFIX}/services").status_code == 200
        assert test_client.post(f"{API_PREFIX}/checks/run", json={}).status_code == 401
        authorized = test_client.post(
            f"{API_PREFIX}/checks/run",
            json={"service_ids": [service["id"]]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert authorized.status_code == 200
        assert test_client.get(f"{API_PREFIX}/operations/audit").status_code == 401
        audit = test_client.get(
            f"{API_PREFIX}/operations/audit", headers={"X-API-Key": api_key}
        ).json()
        assert any(event["actor"] == "api-key-admin" for event in audit)
        config = test_client.get(f"{API_PREFIX}/config").json()
        assert config["authentication_required"] is True


def test_cors_metrics_toggle_and_readiness_failure(
    settings: Settings,
    default_http_handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    configured = replace(
        settings,
        cors_origins=("https://portfolio.example",),
        metrics_enabled=False,
    )
    application = create_app(
        configured,
        monitor_transport=httpx.MockTransport(default_http_handler),
    )
    with TestClient(application) as test_client:
        preflight = test_client.options(
            f"{API_PREFIX}/services",
            headers={
                "Origin": "https://portfolio.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-API-Key",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == (
            "https://portfolio.example"
        )
        assert test_client.get("/metrics").status_code == 404

        application.state.database.ping = AsyncMock(return_value=False)
        not_ready = test_client.get(f"{API_PREFIX}/health/ready")
        assert not_ready.status_code == 503
        assert not_ready.json()["detail"] == "Database unavailable"

        application.state.database.ping = AsyncMock(return_value=True)
        application.state.database.schema_ready = AsyncMock(return_value=False)
        missing_schema = test_client.get(f"{API_PREFIX}/health/ready")
        assert missing_schema.status_code == 503

        application.state.database.ping = AsyncMock(
            side_effect=RuntimeError("connection unavailable")
        )
        readiness_error = test_client.get(f"{API_PREFIX}/health/ready")
        assert readiness_error.status_code == 503


def test_api_reads_persisted_scheduler_heartbeat(
    settings: Settings,
    default_http_handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    configured = replace(
        settings,
        scheduler_enabled=True,
        scheduler_poll_seconds=0.01,
    )
    application = create_app(
        configured,
        monitor_transport=httpx.MockTransport(default_http_handler),
    )
    with TestClient(application) as test_client:
        status = test_client.get(f"{API_PREFIX}/operations/scheduler").json()
        assert status["enabled"] is True
        assert status["running"] is True
        assert status["heartbeat_at"] is not None
        assert (
            test_client.get(f"{API_PREFIX}/config").json()["scheduler_enabled"] is True
        )


def test_unexpected_errors_return_safe_correlated_response(
    settings: Settings,
    default_http_handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    application = create_app(
        settings,
        monitor_transport=httpx.MockTransport(default_http_handler),
    )
    with TestClient(application, raise_server_exceptions=False) as test_client:
        application.state.repository.list_services = AsyncMock(
            side_effect=RuntimeError("sensitive database detail")
        )
        response = test_client.get(
            f"{API_PREFIX}/services",
            headers={"X-Request-ID": "safe-error-1"},
        )
        assert response.status_code == 500
        assert response.json() == {
            "detail": "Internal server error",
            "request_id": "safe-error-1",
        }
        assert "sensitive database detail" not in response.text
        assert response.headers["x-request-id"] == "safe-error-1"
        assert response.headers["cache-control"] == "no-store"


def test_https_response_enables_hsts(
    settings: Settings,
    default_http_handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    application = create_app(
        settings,
        monitor_transport=httpx.MockTransport(default_http_handler),
    )
    with TestClient(application, base_url="https://testserver") as test_client:
        response = test_client.get(f"{API_PREFIX}/health/live")
        assert response.headers["strict-transport-security"] == (
            "max-age=31536000; includeSubDomains"
        )


def test_public_scheduler_status_sanitizes_internal_error(
    settings: Settings,
    default_http_handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    application = create_app(
        settings,
        monitor_transport=httpx.MockTransport(default_http_handler),
    )
    with TestClient(application) as test_client:
        application.state.scheduler.last_error = (
            "postgresql://user:password@private-db/internal"
        )
        response = test_client.get(f"{API_PREFIX}/operations/scheduler")
        assert response.status_code == 200
        assert response.json()["last_error"] == (
            "Scheduler cycle failed; inspect worker logs"
        )
        assert "password" not in response.text
