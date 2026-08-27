"""Asynchronous API checking engine with bounded concurrency and retries."""

from __future__ import annotations

import asyncio
import ipaddress
from time import perf_counter
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.models import CheckResultOut, ServiceOut
from app.repository import Repository


class ServiceSelectionError(ValueError):
    def __init__(self, service_ids: list[int]) -> None:
        self.service_ids = service_ids
        super().__init__(f"Services not found or inactive: {service_ids}")


def validate_target(url: str, *, allow_private_hosts: bool) -> None:
    """Reject obvious server-side request forgery targets by default."""

    hostname = urlsplit(url).hostname
    if hostname is None:
        raise ValueError("Target URL has no hostname")
    if allow_private_hosts:
        return

    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        raise ValueError("Localhost targets are disabled")
    if normalized.endswith(".local") or normalized.endswith(".internal"):
        raise ValueError("Private network targets are disabled")

    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return

    if not address.is_global:
        raise ValueError("Private or reserved IP targets are disabled")


class ApiMonitor:
    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.transport = transport

    async def run_checks(
        self,
        *,
        service_ids: list[int] | None = None,
        concurrency: int | None = None,
    ) -> list[CheckResultOut]:
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
            "User-Agent": "PulseWatch/1.0 (+API reliability monitor)",
            "Accept": "*/*",
        }
        async with httpx.AsyncClient(
            transport=self.transport,
            headers=headers,
            follow_redirects=True,
        ) as client:

            async def guarded_check(service: ServiceOut) -> CheckResultOut:
                async with semaphore:
                    return await self._check_service(client, service)

            return await asyncio.gather(
                *(guarded_check(service) for service in services)
            )

    async def _check_service(
        self, client: httpx.AsyncClient, service: ServiceOut
    ) -> CheckResultOut:
        try:
            validate_target(
                service.url,
                allow_private_hosts=self.settings.allow_private_hosts,
            )
        except ValueError as exc:
            return await self.repository.add_check_result(
                service_id=service.id,
                status_code=None,
                latency_ms=None,
                is_healthy=False,
                attempts=1,
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
                response = await client.request(
                    service.method,
                    service.url,
                    timeout=service.timeout_seconds,
                )
                latency_ms = round((perf_counter() - started) * 1_000, 2)
                status_code = response.status_code

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
                error_message = str(exc)[:240] or "Network request failed"
                retryable = True

            if not retryable or attempt == attempts_allowed:
                break

            delay = self.settings.retry_backoff_seconds * (2 ** (attempt - 1))
            if delay:
                await asyncio.sleep(delay)

        is_healthy = status_code == service.expected_status and error_type is None
        return await self.repository.add_check_result(
            service_id=service.id,
            status_code=status_code,
            latency_ms=latency_ms,
            is_healthy=is_healthy,
            attempts=attempts_made,
            error_type=error_type,
            error_message=error_message,
        )
