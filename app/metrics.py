"""Application-local Prometheus metrics registry."""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class AppMetrics:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.registry = CollectorRegistry(auto_describe=True)
        self.http_requests = Counter(
            "pulsewatch_http_requests_total",
            "HTTP requests handled by the application",
            ("method", "route", "status"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "pulsewatch_http_request_duration_seconds",
            "HTTP request duration",
            ("method", "route"),
            registry=self.registry,
        )
        self.checks = Counter(
            "pulsewatch_checks_total",
            "API checks executed",
            ("trigger", "outcome"),
            registry=self.registry,
        )
        self.check_latency = Histogram(
            "pulsewatch_check_latency_seconds",
            "Observed API response latency",
            ("service_id",),
            registry=self.registry,
        )
        self.incident_events = Counter(
            "pulsewatch_incident_events_total",
            "Incident lifecycle transitions",
            ("event_type",),
            registry=self.registry,
        )
        self.webhook_deliveries = Counter(
            "pulsewatch_webhook_deliveries_total",
            "Webhook delivery attempts by final outcome",
            ("outcome",),
            registry=self.registry,
        )
        self.scheduler_cycles = Counter(
            "pulsewatch_scheduler_cycles_total",
            "Scheduler cycles by outcome",
            ("outcome",),
            registry=self.registry,
        )
        self.scheduler_running = Gauge(
            "pulsewatch_scheduler_running",
            "Whether the scheduler background loop is running",
            registry=self.registry,
        )

    def observe_http(
        self, *, method: str, route: str, status: int, duration_seconds: float
    ) -> None:
        if not self.enabled:
            return
        self.http_requests.labels(method=method, route=route, status=str(status)).inc()
        self.http_duration.labels(method=method, route=route).observe(duration_seconds)

    def observe_check(
        self,
        *,
        service_id: int,
        trigger: str,
        healthy: bool,
        latency_ms: float | None,
    ) -> None:
        if not self.enabled:
            return
        outcome = "healthy" if healthy else "failed"
        self.checks.labels(trigger=trigger, outcome=outcome).inc()
        if latency_ms is not None:
            self.check_latency.labels(service_id=str(service_id)).observe(
                latency_ms / 1_000
            )

    def render(self) -> bytes:
        return generate_latest(self.registry)
