"""Persistence layer for monitored services and check results."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from app.database import Database
from app.models import (
    CheckResultOut,
    ServiceCreate,
    ServiceMetric,
    ServiceOut,
    ServiceUpdate,
    SummaryOut,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _service_from_row(row: aiosqlite.Row) -> ServiceOut:
    data = dict(row)
    data["active"] = bool(data["active"])
    return ServiceOut.model_validate(data)


def _result_from_row(row: aiosqlite.Row) -> CheckResultOut:
    data = dict(row)
    data["is_healthy"] = bool(data["is_healthy"])
    return CheckResultOut.model_validate(data)


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self._write_lock = asyncio.Lock()

    async def create_service(self, payload: ServiceCreate) -> ServiceOut:
        now = utc_now().isoformat()
        values = payload.model_dump()
        async with self._write_lock, self.database.connect() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO services (
                    name, url, method, expected_status, timeout_seconds,
                    retry_count, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["name"],
                    values["url"],
                    values["method"],
                    values["expected_status"],
                    values["timeout_seconds"],
                    values["retry_count"],
                    int(values["active"]),
                    now,
                    now,
                ),
            )
            await connection.commit()
            service_id = cursor.lastrowid

        service = await self.get_service(int(service_id))
        if service is None:  # pragma: no cover - defensive database guard
            raise RuntimeError("Created service could not be loaded")
        return service

    async def list_services(
        self,
        *,
        active_only: bool = False,
        service_ids: list[int] | None = None,
    ) -> list[ServiceOut]:
        clauses: list[str] = []
        parameters: list[Any] = []

        if active_only:
            clauses.append("active = 1")
        if service_ids is not None:
            if not service_ids:
                return []
            placeholders = ", ".join("?" for _ in service_ids)
            clauses.append(f"id IN ({placeholders})")
            parameters.extend(service_ids)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM services{where} ORDER BY name COLLATE NOCASE, id"
        async with self.database.connect() as connection:
            cursor = await connection.execute(query, parameters)
            rows = await cursor.fetchall()
        return [_service_from_row(row) for row in rows]

    async def get_service(self, service_id: int) -> ServiceOut | None:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM services WHERE id = ?", (service_id,)
            )
            row = await cursor.fetchone()
        return None if row is None else _service_from_row(row)

    async def update_service(
        self, service_id: int, payload: ServiceUpdate
    ) -> ServiceOut | None:
        values = payload.model_dump(exclude_unset=True)
        if "active" in values:
            values["active"] = int(values["active"])
        values["updated_at"] = utc_now().isoformat()

        assignments = ", ".join(f"{column} = ?" for column in values)
        parameters = [*values.values(), service_id]
        async with self._write_lock, self.database.connect() as connection:
            cursor = await connection.execute(
                f"UPDATE services SET {assignments} WHERE id = ?", parameters
            )
            await connection.commit()
            if cursor.rowcount == 0:
                return None
        return await self.get_service(service_id)

    async def delete_service(self, service_id: int) -> bool:
        async with self._write_lock, self.database.connect() as connection:
            cursor = await connection.execute(
                "DELETE FROM services WHERE id = ?", (service_id,)
            )
            await connection.commit()
        return cursor.rowcount > 0

    async def add_check_result(
        self,
        *,
        service_id: int,
        status_code: int | None,
        latency_ms: float | None,
        is_healthy: bool,
        attempts: int,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> CheckResultOut:
        checked_at = utc_now().isoformat()
        async with self._write_lock, self.database.connect() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO check_results (
                    service_id, checked_at, status_code, latency_ms,
                    is_healthy, attempts, error_type, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    service_id,
                    checked_at,
                    status_code,
                    latency_ms,
                    int(is_healthy),
                    attempts,
                    error_type,
                    error_message,
                ),
            )
            await connection.commit()
            result_id = cursor.lastrowid

            result_cursor = await connection.execute(
                "SELECT * FROM check_results WHERE id = ?", (result_id,)
            )
            row = await result_cursor.fetchone()

        if row is None:  # pragma: no cover - defensive database guard
            raise RuntimeError("Created check result could not be loaded")
        return _result_from_row(row)

    async def list_check_results(
        self, *, service_id: int | None = None, limit: int = 50
    ) -> list[CheckResultOut]:
        if service_id is None:
            query = (
                "SELECT * FROM check_results ORDER BY checked_at DESC, id DESC LIMIT ?"
            )
            parameters: tuple[Any, ...] = (limit,)
        else:
            query = """
                SELECT * FROM check_results
                WHERE service_id = ?
                ORDER BY checked_at DESC, id DESC
                LIMIT ?
            """
            parameters = (service_id, limit)

        async with self.database.connect() as connection:
            cursor = await connection.execute(query, parameters)
            rows = await cursor.fetchall()
        return [_result_from_row(row) for row in rows]

    async def build_summary(
        self, *, window_hours: int, degraded_latency_ms: float
    ) -> SummaryOut:
        cutoff = (utc_now() - timedelta(hours=window_hours)).isoformat()
        query = """
        WITH recent AS (
            SELECT * FROM check_results WHERE checked_at >= ?
        ),
        stats AS (
            SELECT
                service_id,
                COUNT(*) AS checks,
                SUM(is_healthy) AS healthy_checks,
                SUM(CASE WHEN is_healthy = 0 THEN 1 ELSE 0 END) AS incidents,
                AVG(latency_ms) AS average_latency_ms
            FROM recent
            GROUP BY service_id
        ),
        latest_ranked AS (
            SELECT
                check_results.*,
                ROW_NUMBER() OVER (
                    PARTITION BY service_id
                    ORDER BY checked_at DESC, id DESC
                ) AS row_number
            FROM check_results
        )
        SELECT
            services.id,
            services.name,
            services.url,
            COALESCE(stats.checks, 0) AS checks,
            COALESCE(stats.healthy_checks, 0) AS healthy_checks,
            COALESCE(stats.incidents, 0) AS incidents,
            stats.average_latency_ms,
            latest_ranked.checked_at AS last_checked_at,
            latest_ranked.status_code AS last_status_code,
            latest_ranked.latency_ms AS last_latency_ms,
            latest_ranked.is_healthy AS last_is_healthy
        FROM services
        LEFT JOIN stats ON stats.service_id = services.id
        LEFT JOIN latest_ranked
            ON latest_ranked.service_id = services.id
            AND latest_ranked.row_number = 1
        WHERE services.active = 1
        ORDER BY services.name COLLATE NOCASE, services.id
        """

        async with self.database.connect() as connection:
            cursor = await connection.execute(query, (cutoff,))
            rows = await cursor.fetchall()

        services: list[ServiceMetric] = []
        checks_total = 0
        healthy_checks_total = 0
        incidents_total = 0
        weighted_latency_sum = 0.0
        latency_observations = 0
        state_counts = {
            "healthy": 0,
            "degraded": 0,
            "down": 0,
            "never_checked": 0,
        }

        for row in rows:
            checks = int(row["checks"])
            healthy_checks = int(row["healthy_checks"])
            incidents = int(row["incidents"])
            last_is_healthy = row["last_is_healthy"]
            last_latency = row["last_latency_ms"]

            if last_is_healthy is None:
                state = "never_checked"
            elif not bool(last_is_healthy):
                state = "down"
            elif last_latency is not None and last_latency > degraded_latency_ms:
                state = "degraded"
            else:
                state = "healthy"

            state_counts[state] += 1
            checks_total += checks
            healthy_checks_total += healthy_checks
            incidents_total += incidents
            if row["average_latency_ms"] is not None:
                weighted_latency_sum += float(row["average_latency_ms"]) * checks
                latency_observations += checks

            services.append(
                ServiceMetric(
                    id=row["id"],
                    name=row["name"],
                    url=row["url"],
                    state=state,
                    last_checked_at=row["last_checked_at"],
                    last_status_code=row["last_status_code"],
                    last_latency_ms=(
                        round(float(last_latency), 2)
                        if last_latency is not None
                        else None
                    ),
                    checks=checks,
                    uptime_percentage=(
                        round(healthy_checks * 100 / checks, 2) if checks else None
                    ),
                )
            )

        return SummaryOut(
            generated_at=utc_now(),
            window_hours=window_hours,
            services_total=len(services),
            healthy_services=state_counts["healthy"],
            degraded_services=state_counts["degraded"],
            down_services=state_counts["down"],
            never_checked=state_counts["never_checked"],
            checks_total=checks_total,
            incidents_total=incidents_total,
            uptime_percentage=(
                round(healthy_checks_total * 100 / checks_total, 2)
                if checks_total
                else None
            ),
            average_latency_ms=(
                round(weighted_latency_sum / latency_observations, 2)
                if latency_observations
                else None
            ),
            services=services,
        )
