from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import create_service


def test_health_dashboard_docs_and_observability_headers(client: TestClient) -> None:
    health = client.get("/api/health", headers={"X-Request-ID": "portfolio-test"})
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "version": "1.0.0",
        "database": "connected",
    }
    assert health.headers["x-request-id"] == "portfolio-test"
    assert "app;dur=" in health.headers["server-timing"]
    assert health.headers["x-content-type-options"] == "nosniff"

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "PulseWatch" in dashboard.text
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["info"]["version"] == "1.0.0"


def test_service_crud_contract(client: TestClient) -> None:
    created = create_service(client)
    assert created["id"] == 1
    assert created["active"] is True
    assert created["expected_status"] == 200

    fetched = client.get(f"/api/services/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["url"] == "https://service.test/healthy"

    updated = client.patch(
        f"/api/services/{created['id']}",
        json={"name": "Renamed API", "retry_count": 1, "active": False},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed API"
    assert updated.json()["retry_count"] == 1
    assert updated.json()["active"] is False

    assert len(client.get("/api/services").json()) == 1
    assert client.get("/api/services?active_only=true").json() == []

    deleted = client.delete(f"/api/services/{created['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/api/services/{created['id']}").status_code == 404
    assert client.delete(f"/api/services/{created['id']}").status_code == 404


def test_duplicate_url_returns_conflict(client: TestClient) -> None:
    create_service(client, name="First")
    response = client.post(
        "/api/services",
        json={"name": "Duplicate", "url": "https://service.test/healthy"},
    )
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_service_validation_errors_are_explicit(client: TestClient) -> None:
    invalid_url = client.post(
        "/api/services",
        json={"name": "Invalid", "url": "ftp://example.com/data"},
    )
    assert invalid_url.status_code == 422
    assert "http or https" in invalid_url.text

    credentials = client.post(
        "/api/services",
        json={"name": "Unsafe", "url": "https://user:secret@example.com"},
    )
    assert credentials.status_code == 422
    assert "credentials" in credentials.text

    empty_patch = client.patch("/api/services/999", json={})
    assert empty_patch.status_code == 422


def test_missing_service_routes_return_404(client: TestClient) -> None:
    assert client.get("/api/services/99").status_code == 404
    assert client.patch("/api/services/99", json={"active": False}).status_code == 404


def test_run_checks_records_success_and_retryable_failure(client: TestClient) -> None:
    healthy = create_service(client, name="Healthy")
    failing = create_service(client, name="Failing", url="https://service.test/failing")

    response = client.post("/api/checks/run", json={})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["healthy"] == 1
    assert payload["failed"] == 1

    results = {item["service_id"]: item for item in payload["results"]}
    assert results[healthy["id"]]["is_healthy"] is True
    assert results[healthy["id"]]["attempts"] == 1
    assert results[failing["id"]]["is_healthy"] is False
    assert results[failing["id"]]["status_code"] == 503
    assert results[failing["id"]]["attempts"] == 3
    assert results[failing["id"]]["error_type"] == "UnexpectedStatus"


def test_non_retryable_status_is_checked_once(client: TestClient) -> None:
    service = create_service(
        client, name="Missing route", url="https://service.test/missing"
    )
    result = client.post(
        "/api/checks/run", json={"service_ids": [service["id"]]}
    ).json()["results"][0]
    assert result["status_code"] == 404
    assert result["attempts"] == 1
    assert result["error_message"] == "Expected HTTP 200, received 404"


def test_expected_non_200_status_can_be_healthy(client: TestClient) -> None:
    service = create_service(
        client,
        name="Create endpoint",
        url="https://service.test/created",
        expected_status=201,
    )
    result = client.post(
        "/api/checks/run", json={"service_ids": [service["id"]]}
    ).json()["results"][0]
    assert result["status_code"] == 201
    assert result["is_healthy"] is True


def test_timeout_is_normalized_and_retried(client: TestClient) -> None:
    service = create_service(
        client,
        name="Slow API",
        url="https://service.test/timeout",
        retry_count=1,
    )
    result = client.post(
        "/api/checks/run", json={"service_ids": [service["id"]]}
    ).json()["results"][0]
    assert result["is_healthy"] is False
    assert result["attempts"] == 2
    assert result["error_type"] == "Timeout"
    assert "timeout" in result["error_message"]


def test_private_ip_target_is_blocked_before_request(client: TestClient) -> None:
    service = create_service(
        client, name="Metadata", url="http://169.254.169.254/latest/meta-data"
    )
    result = client.post(
        "/api/checks/run", json={"service_ids": [service["id"]]}
    ).json()["results"][0]
    assert result["is_healthy"] is False
    assert result["status_code"] is None
    assert result["error_type"] == "BlockedTarget"


def test_selected_check_rejects_missing_and_inactive_ids(client: TestClient) -> None:
    service = create_service(client)
    missing = client.post("/api/checks/run", json={"service_ids": [99]})
    assert missing.status_code == 404
    assert missing.json()["detail"]["service_ids"] == [99]

    client.patch(f"/api/services/{service['id']}", json={"active": False})
    inactive = client.post("/api/checks/run", json={"service_ids": [service["id"]]})
    assert inactive.status_code == 404


def test_empty_database_check_run_is_valid(client: TestClient) -> None:
    response = client.post("/api/checks/run")
    assert response.status_code == 200
    assert response.json() == {"total": 0, "healthy": 0, "failed": 0, "results": []}


def test_history_summary_filtering_and_cascade_delete(client: TestClient) -> None:
    first = create_service(client, name="First")
    second = create_service(
        client, name="Second", url="https://service-two.test/healthy"
    )
    client.post("/api/checks/run", json={})

    history = client.get("/api/checks?limit=10").json()
    assert len(history) == 2
    filtered = client.get(f"/api/checks?service_id={first['id']}").json()
    assert len(filtered) == 1
    assert filtered[0]["service_id"] == first["id"]

    summary = client.get("/api/summary?window_hours=24").json()
    assert summary["services_total"] == 2
    assert summary["healthy_services"] == 2
    assert summary["checks_total"] == 2
    assert summary["uptime_percentage"] == 100.0
    assert summary["incidents_total"] == 0

    client.delete(f"/api/services/{second['id']}")
    remaining_history = client.get("/api/checks?limit=10").json()
    assert len(remaining_history) == 1
    assert remaining_history[0]["service_id"] == first["id"]


def test_query_validation_limits(client: TestClient) -> None:
    assert client.get("/api/checks?limit=0").status_code == 422
    assert client.get("/api/checks?limit=501").status_code == 422
    assert client.get("/api/summary?window_hours=0").status_code == 422
    assert client.post("/api/checks/run", json={"service_ids": [-1]}).status_code == 422
