"""FastAPI application factory and HTTP routes."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

import httpx
from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import Settings
from app.database import Database
from app.models import (
    CheckResultOut,
    HealthOut,
    RunChecksRequest,
    RunChecksResponse,
    ServiceCreate,
    ServiceOut,
    ServiceUpdate,
    SummaryOut,
)
from app.monitoring import ApiMonitor, ServiceSelectionError
from app.repository import Repository

logger = logging.getLogger("pulsewatch")
STATIC_DIRECTORY = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    monitor_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings.from_env()
    database = Database(runtime_settings.database_path)
    repository = Repository(database)
    monitor = ApiMonitor(repository, runtime_settings, transport=monitor_transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await database.initialize()
        logger.info("PulseWatch database initialized at %s", database.path)
        yield

    application = FastAPI(
        title="PulseWatch API",
        description=(
            "Asynchronous API reliability monitoring with retries, persistence, "
            "metrics, and a lightweight operations dashboard."
        ),
        version=__version__,
        lifespan=lifespan,
        contact={
            "name": "Mario Fuentes",
            "url": "https://github.com/TheKhadaJhin",
        },
        license_info={"name": "MIT"},
    )
    application.state.settings = runtime_settings
    application.state.database = database
    application.state.repository = repository
    application.state.monitor = monitor

    @application.middleware("http")
    async def observability_headers(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))[:128]
        started = perf_counter()
        response = await call_next(request)
        elapsed_ms = (perf_counter() - started) * 1_000
        response.headers["X-Request-ID"] = request_id
        response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.2f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @application.exception_handler(sqlite3.IntegrityError)
    async def handle_integrity_error(_: Request, exc: sqlite3.IntegrityError):
        logger.info("Database constraint rejected a request: %s", exc)
        return JSONResponse(
            status_code=409,
            content={"detail": "A service with this URL already exists."},
        )

    router = APIRouter(prefix="/api")

    @router.get("/health", response_model=HealthOut, tags=["System"])
    async def health() -> HealthOut:
        if not await database.ping():  # pragma: no cover - defensive branch
            raise HTTPException(status_code=503, detail="Database unavailable")
        return HealthOut(status="ok", version=__version__, database="connected")

    @router.post(
        "/services",
        response_model=ServiceOut,
        status_code=201,
        tags=["Services"],
    )
    async def create_service(payload: ServiceCreate) -> ServiceOut:
        return await repository.create_service(payload)

    @router.get("/services", response_model=list[ServiceOut], tags=["Services"])
    async def list_services(
        active_only: bool = Query(default=False),
    ) -> list[ServiceOut]:
        return await repository.list_services(active_only=active_only)

    @router.get("/services/{service_id}", response_model=ServiceOut, tags=["Services"])
    async def get_service(service_id: int) -> ServiceOut:
        service = await repository.get_service(service_id)
        if service is None:
            raise HTTPException(status_code=404, detail="Service not found")
        return service

    @router.patch(
        "/services/{service_id}", response_model=ServiceOut, tags=["Services"]
    )
    async def update_service(service_id: int, payload: ServiceUpdate) -> ServiceOut:
        service = await repository.update_service(service_id, payload)
        if service is None:
            raise HTTPException(status_code=404, detail="Service not found")
        return service

    @router.delete("/services/{service_id}", status_code=204, tags=["Services"])
    async def delete_service(service_id: int) -> Response:
        deleted = await repository.delete_service(service_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Service not found")
        return Response(status_code=204)

    @router.post(
        "/checks/run",
        response_model=RunChecksResponse,
        tags=["Checks"],
    )
    async def run_checks(
        payload: RunChecksRequest = Body(default_factory=RunChecksRequest),
    ) -> RunChecksResponse:
        try:
            results = await monitor.run_checks(
                service_ids=payload.service_ids,
                concurrency=payload.concurrency,
            )
        except ServiceSelectionError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "Some services were not found or are inactive.",
                    "service_ids": exc.service_ids,
                },
            ) from exc

        healthy = sum(result.is_healthy for result in results)
        return RunChecksResponse(
            total=len(results),
            healthy=healthy,
            failed=len(results) - healthy,
            results=results,
        )

    @router.get("/checks", response_model=list[CheckResultOut], tags=["Checks"])
    async def list_checks(
        service_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[CheckResultOut]:
        return await repository.list_check_results(service_id=service_id, limit=limit)

    @router.get("/summary", response_model=SummaryOut, tags=["Metrics"])
    async def summary(
        window_hours: int = Query(default=24, ge=1, le=720),
    ) -> SummaryOut:
        return await repository.build_summary(
            window_hours=window_hours,
            degraded_latency_ms=runtime_settings.degraded_latency_ms,
        )

    application.include_router(router)
    application.mount(
        "/",
        StaticFiles(directory=STATIC_DIRECTORY, html=True),
        name="dashboard",
    )
    return application


app = create_app()
