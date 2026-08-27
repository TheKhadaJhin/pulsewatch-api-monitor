"""Lifecycle-aware background scheduler and retention worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.config import Settings
from app.db_models import utc_now
from app.metrics import AppMetrics
from app.models import SchedulerStatusOut
from app.monitoring import ApiMonitor
from app.repository import Repository

logger = logging.getLogger("pulsewatch.scheduler")


class CheckScheduler:
    def __init__(
        self,
        repository: Repository,
        monitor: ApiMonitor,
        settings: Settings,
        metrics: AppMetrics,
    ) -> None:
        self.repository = repository
        self.monitor = monitor
        self.settings = settings
        self.metrics = metrics
        self._task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        self._publish_lock = asyncio.Lock()
        self.heartbeat_at: datetime | None = None
        self.last_cycle_started_at: datetime | None = None
        self.last_cycle_finished_at: datetime | None = None
        self.last_error: str | None = None
        self.checks_executed = 0
        self.retention_last_run_at: datetime | None = None
        self._next_retention_at = utc_now()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def heartbeat_interval_seconds(self) -> float:
        return min(10.0, max(1.0, self.settings.scheduler_poll_seconds))

    async def start(self) -> None:
        if not self.settings.scheduler_enabled or self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="pulsewatch-scheduler")
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="pulsewatch-scheduler-heartbeat"
        )
        self.metrics.scheduler_running.set(1)
        await self._publish_status()
        logger.info("Scheduler started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await self._task
            if self._heartbeat_task is not None:
                await self._heartbeat_task
        finally:
            self._task = None
            self._heartbeat_task = None
            self.metrics.scheduler_running.set(0)
            await self._publish_status(force=True)
            logger.info("Scheduler stopped")

    async def run_once(self) -> int:
        if self._cycle_lock.locked():
            return 0
        async with self._cycle_lock:
            self.last_cycle_started_at = utc_now()
            await self._publish_status()
            try:
                due_services = await self.repository.list_services(
                    active_only=True,
                    due_before=self.last_cycle_started_at,
                    limit=self.settings.scheduler_batch_size,
                )
                results = (
                    await self.monitor.run_checks(
                        service_ids=[service.id for service in due_services],
                        trigger="scheduled",
                    )
                    if due_services
                    else []
                )
                self.checks_executed += len(results)
                await self._run_retention_if_due()
                self.last_error = None
                self.metrics.scheduler_cycles.labels(outcome="success").inc()
                return len(results)
            except Exception as exc:
                self.last_error = str(exc)[:500]
                self.metrics.scheduler_cycles.labels(outcome="failed").inc()
                logger.exception("Scheduler cycle failed")
                return 0
            finally:
                self.last_cycle_finished_at = utc_now()
                await self._publish_status()

    async def _run_retention_if_due(self) -> None:
        now = utc_now()
        if now < self._next_retention_at:
            return
        cutoff = now - timedelta(days=self.settings.retention_days)
        deleted = await self.repository.purge_history(before=cutoff)
        self.retention_last_run_at = now
        self._next_retention_at = now + timedelta(
            seconds=self.settings.retention_interval_seconds
        )
        logger.info("Retention removed %s expired check result(s)", deleted)

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.settings.scheduler_poll_seconds,
                )
            except TimeoutError:
                continue

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._publish_status()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.heartbeat_interval_seconds,
                )
            except TimeoutError:
                continue

    async def _publish_status(self, *, force: bool = False) -> None:
        if not self.settings.scheduler_enabled:
            return
        if self._task is None and not force:
            return
        self.heartbeat_at = utc_now()
        async with self._publish_lock:
            try:
                await self.repository.save_scheduler_status(self.status())
            except Exception:
                logger.exception("Could not persist scheduler heartbeat")

    def status(self) -> SchedulerStatusOut:
        return SchedulerStatusOut(
            enabled=self.settings.scheduler_enabled,
            running=self.running,
            cycle_in_progress=self._cycle_lock.locked(),
            poll_seconds=self.settings.scheduler_poll_seconds,
            heartbeat_at=self.heartbeat_at,
            last_cycle_started_at=self.last_cycle_started_at,
            last_cycle_finished_at=self.last_cycle_finished_at,
            last_error=self.last_error,
            checks_executed=self.checks_executed,
            retention_last_run_at=self.retention_last_run_at,
        )
