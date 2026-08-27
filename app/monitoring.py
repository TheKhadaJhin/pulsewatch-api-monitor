"""Asynchronous API checking engine with bounded concurrency and retries."""

from __future__ import annotations

import asyncio
import secrets
from time import perf_counter

import httpx

from app.config import Settings
from app.metrics import AppMetrics
from app.models import CheckResultOut, CheckTrigger, ServiceOut
from app.notifications import WebhookNotifier
from app.repository import RecordedCheck, Repository
from app.security import validate_target

_JITTER_RANDOM = secrets.SystemRandom()


class ServiceSelectionError(ValueError):
    def __init__(self, service_ids: list[int]) -> None:
        self.service_ids = service_ids
        super().__init__(f"Services not found or inactive: {service_ids}")


class ApiMonitor:
    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        notifier: WebhookNotifier,
        metrics: AppMetrics,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.notifier = notifier
        self.metrics = metrics
        self.transport = transport
        self._batch_lock = asyncio.Lock()

    async def run_checks(
        self,
        *,
        service_ids: list[int] | None = None,
        concurrency: int | None = None,
        trigger: CheckTrigger = "manual",
        request_id: str | None = None,
    ) -> list[CheckResultOut]:
        async with self._batch_lock:
            services = await self.repository.list_services(
                active_only=True, service_ids=service_ids
            )
            if service_ids is not None:
                selected_ids = {service.id for service in services}
                unavailable = [item for item in service_ids if item not in selected_ids]
                if unavailable:
                    raise ServiceSelectionError(unavailable)
            if not services:
                return []

            limit = concurrency or self.settings.check_concurrency
            semaphore = asyncio.Semaphore(limit)
            headers = {
                "User-Agent": "PulseWatch/2.0 (+API reliability monitor)",
                "Accept": "*/*",
            }
            async with httpx.AsyncClient(
                transport=self.transport,
                headers=headers,
                # Redirects are not followed because a public URL can redirect the
                # monitor into a private network and bypass target validation.
                follow_redirects=False,
                # Environment proxies can change the actual network destination and
                # weaken target-policy assumptions. Proxy support should be explicit.
                trust_env=False,
            ) as client:

                async def guarded_check(service: ServiceOut) -> CheckResultOut:
                    async with semaphore:
                        return await self._check_service(
                            client,
                            service,
                            trigger=trigger,
                            request_id=request_id,
                        )

                return await asyncio.gather(
                    *(guarded_check(service) for service in services)
                )

    async def _record(
        self,
        *,
        service: ServiceOut,
        status_code: int | None,
        latency_ms: float | None,
        is_healthy: bool,
        attempts: int,
        trigger: CheckTrigger,
        request_id: str | None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> CheckResultOut:
        try:
            recorded: RecordedCheck = await self.repository.record_check_result(
                service=service,
                status_code=status_code,
                latency_ms=latency_ms,
                is_healthy=is_healthy,
                attempts=attempts,
                trigger=trigger,
                error_type=error_type,
                error_message=error_message,
                request_id=request_id,
            )
        except LookupError as exc:
            raise ServiceSelectionError([service.id]) from exc
        self.metrics.observe_check(
            service_id=service.id,
            trigger=trigger,
            healthy=is_healthy,
            latency_ms=latency_ms,
        )
        if recorded.transition is not None:
            self.metrics.incident_events.labels(
                event_type=recorded.transition.event_type
            ).inc()
            await self.notifier.notify(recorded.transition)
        return recorded.result

    async def _check_service(
        self,
        client: httpx.AsyncClient,
        service: ServiceOut,
        *,
        trigger: CheckTrigger,
        request_id: str | None,
    ) -> CheckResultOut:
        try:
            validate_target(
                service.url,
                allow_private_hosts=self.settings.allow_private_hosts,
            )
        except ValueError as exc:
            return await self._record(
                service=service,
                status_code=None,
                latency_ms=None,
                is_healthy=False,
                attempts=1,
                trigger=trigger,
                request_id=request_id,
                error_type="BlockedTarget",
                error_message=str(exc),
            )

        attempts_allowed = service.retry_count + 1
        status_code: int | None = None
        latency_ms: float | None = None
        error_type: str | None = None
        error_message: str | None = None
        attempts_made = 0

        for attempt in range(1, attempts_allowed + 1):
            attempts_made = attempt
            started = perf_counter()
            retryable = False
            try:
                async with client.stream(
                    service.method,
                    service.url,
                    timeout=service.timeout_seconds,
                ) as response:
                    status_code = response.status_code
                latency_ms = round((perf_counter() - started) * 1_000, 2)
                if status_code == service.expected_status:
                    error_type = None
                    error_message = None
                    break
                error_type = "UnexpectedStatus"
                error_message = (
                    f"Expected HTTP {service.expected_status}, received {status_code}"
                )
                retryable = status_code == 429 or status_code >= 500
            except httpx.TimeoutException:
                latency_ms = round((perf_counter() - started) * 1_000, 2)
                error_type = "Timeout"
                error_message = (
                    f"Request exceeded the {service.timeout_seconds:g}s timeout"
                )
                retryable = True
            except httpx.RequestError as exc:
                latency_ms = round((perf_counter() - started) * 1_000, 2)
                error_type = type(exc).__name__
                error_message = str(exc)[:500] or "Network request failed"
                retryable = True

            if not retryable or attempt == attempts_allowed:
                break
            base_delay = self.settings.retry_backoff_seconds * (2 ** (attempt - 1))
            delay = base_delay + _JITTER_RANDOM.uniform(0, base_delay * 0.25)
            if delay:
                await asyncio.sleep(delay)

        is_healthy = status_code == service.expected_status and error_type is None
        return await self._record(
            service=service,
            status_code=status_code,
            latency_ms=latency_ms,
            is_healthy=is_healthy,
            attempts=attempts_made,
            trigger=trigger,
            request_id=request_id,
            error_type=error_type,
            error_message=error_message,
        )


__all__ = ["ApiMonitor", "ServiceSelectionError", "validate_target"]
