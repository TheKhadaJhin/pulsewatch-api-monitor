"""Signed webhook notifications for incident transitions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

import httpx

from app.config import Settings
from app.metrics import AppMetrics
from app.models import IncidentTransition, NotificationDeliveryOut
from app.repository import Repository
from app.security import validate_target

logger = logging.getLogger("pulsewatch.notifications")


class WebhookNotifier:
    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        metrics: AppMetrics,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.metrics = metrics
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.settings.webhook_url)

    async def notify(
        self, transition: IncidentTransition
    ) -> NotificationDeliveryOut | None:
        webhook_url = self.settings.webhook_url
        if webhook_url is None:
            return None

        try:
            validate_target(
                webhook_url,
                allow_private_hosts=self.settings.allow_private_hosts,
            )
        except ValueError as exc:
            self.metrics.webhook_deliveries.labels(outcome="blocked").inc()
            return await self.repository.record_notification_delivery(
                incident_id=transition.incident.id,
                event_type=transition.event_type,
                delivered=False,
                status_code=None,
                attempts=1,
                error_message=str(exc),
            )

        payload = {
            "event": transition.event_type,
            "incident": transition.incident.model_dump(mode="json"),
            "service": transition.service.model_dump(mode="json"),
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "PulseWatch/2.0 (signed incident webhook)",
            "X-PulseWatch-Event": transition.event_type,
            "Idempotency-Key": (
                f"pulsewatch:{transition.incident.id}:{transition.event_type}"
            ),
        }
        if self.settings.webhook_secret:
            signature = hmac.new(
                self.settings.webhook_secret.encode(),
                body,
                hashlib.sha256,
            ).hexdigest()
            headers["X-PulseWatch-Signature"] = f"sha256={signature}"

        delivered = False
        status_code: int | None = None
        error_message: str | None = None
        attempts = 0
        attempts_allowed = self.settings.webhook_retry_count + 1

        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for attempt in range(1, attempts_allowed + 1):
                attempts = attempt
                retryable = False
                try:
                    response = await client.post(
                        webhook_url,
                        content=body,
                        headers=headers,
                        timeout=self.settings.webhook_timeout_seconds,
                    )
                    status_code = response.status_code
                    delivered = 200 <= status_code < 300
                    if delivered:
                        error_message = None
                        break
                    error_message = f"Webhook returned HTTP {status_code}"
                    retryable = status_code == 429 or status_code >= 500
                except httpx.TimeoutException:
                    error_message = "Webhook delivery timed out"
                    retryable = True
                except httpx.RequestError as exc:
                    error_message = str(exc)[:500] or "Webhook network request failed"
                    retryable = True

                if not retryable or attempt == attempts_allowed:
                    break
                await asyncio.sleep(
                    self.settings.retry_backoff_seconds * (2 ** (attempt - 1))
                )

        outcome = "delivered" if delivered else "failed"
        self.metrics.webhook_deliveries.labels(outcome=outcome).inc()
        delivery = await self.repository.record_notification_delivery(
            incident_id=transition.incident.id,
            event_type=transition.event_type,
            delivered=delivered,
            status_code=status_code,
            attempts=attempts,
            error_message=error_message,
        )
        logger.info(
            "Webhook %s for incident %s after %s attempt(s)",
            outcome,
            transition.incident.id,
            attempts,
        )
        return delivery
