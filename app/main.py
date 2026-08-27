"""FastAPI application factory, versioned routes, and production middleware."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from typing import Literal

import httpx
from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import RequestResponseEndpoint

from app import __version__
from app.config import Settings
from app.database import Database
from app.db_models import utc_now
from app.logging_config import configure_logging
from app.metrics import AppMetrics
from app.models import (
    AuditEventOut,
    CheckResultOut,
    CheckTrigger,
    HealthOut,
    IncidentOut,
    NotificationDeliveryOut,
    PublicConfigOut,
    ReadinessOut,
    RetentionResultOut,
    RunChecksRequest,
    RunChecksResponse,
    SchedulerRunOut,
    SchedulerStatusOut,
    ServiceCreate,
    ServiceOut,
    ServiceUpdate,
    SummaryOut,
)
from app.monitoring import ApiMonitor, ServiceSelectionError
from app.notifications import WebhookNotifier
from app.repository import Repository
from app.scheduler import CheckScheduler
from app.security import AdminAuthenticator

logger = logging.getLogger("pulsewatch")
STATIC_DIRECTORY = Path(__file__).parent / "static"
API_PREFIX = "/api/v1"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "unknown"))


def _actor(request: Request) -> str:
    return str(getattr(request.state, "actor", "anonymous"))


def create_app(
    settings: Settings | None = None,
    *,
    monitor_transport: httpx.AsyncBaseTransport | None = None,
    notification_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings.from_env()
    configure_logging(
        level=runtime_settings.log_level, use_json=runtime_settings.log_json
    )
    database = Database(runtime_settings.database_url)
    repository = Repository(database)
    metrics = AppMetrics(enabled=runtime_settings.metrics_enabled)
    notifier = WebhookNotifier(
        repository,
        runtime_settings,
        metrics,
        transport=notification_transport,
    )
    monitor = ApiMonitor(
        repository,
        runtime_settings,
        notifier,
        metrics,
        transport=monitor_transport,
    )
    scheduler = CheckScheduler(repository, monitor, runtime_settings, metrics)
    require_admin = AdminAuthenticator(runtime_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if runtime_settings.auto_create_schema:
            await database.initialize()
        if not await database.ping() or not await database.schema_ready():
            raise RuntimeError("Database readiness check failed")
        await scheduler.start()
        logger.info(
            "PulseWatch %s started in %s mode",
            __version__,
            runtime_settings.environment,
        )
        try:
            yield
        finally:
            await scheduler.stop()
            await database.dispose()

    application = FastAPI(
        title="PulseWatch API",
        summary="Production-minded asynchronous API reliability monitoring",
        description=(
            "Scheduled and on-demand API checks with retries, incident lifecycle "
            "management, signed webhooks, audit history, metrics, and retention."
        ),
        version=__version__,
        lifespan=lifespan,
        contact={
            "name": "Mario Fuentes",
            "url": "https://github.com/TheKhadaJhin",
        },
        license_info={"name": "MIT"},
        openapi_tags=[
            {"name": "System", "description": "Health, readiness, and public config"},
            {"name": "Services", "description": "Monitored endpoint configuration"},
            {"name": "Checks", "description": "Manual checks and historical results"},
            {"name": "Incidents", "description": "Failure and recovery lifecycle"},
            {"name": "Operations", "description": "Scheduler, retention, and auditing"},
        ],
    )
    application.state.settings = runtime_settings
    application.state.database = database
    application.state.repository = repository
    application.state.metrics = metrics
    application.state.notifier = notifier
    application.state.monitor = monitor
    application.state.scheduler = scheduler

    if runtime_settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(runtime_settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-API-Key",
                "X-Request-ID",
            ],
        )

    @application.middleware("http")
    async def request_observability(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_request_id
            if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
            else str(uuid.uuid4())
        )
        request.state.request_id = request_id
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(
                "Unhandled application error",
                exc_info=(type(exc), exc, exc.__traceback__),
                extra={"request_id": request_id},
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal server error",
                    "request_id": request_id,
                },
            )
        elapsed_seconds = perf_counter() - started
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        metrics.observe_http(
            method=request.method,
            route=route_path,
            status=response.status_code,
            duration_seconds=elapsed_seconds,
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["Server-Timing"] = f"app;dur={elapsed_seconds * 1_000:.2f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        if request.url.path.startswith(API_PREFIX) or request.url.path in {
            "/api/health",
            "/metrics",
        }:
            response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/docs") or request.url.path == "/redoc":
            # FastAPI's generated documentation bootstraps its CDN bundle with an
            # inline script. Keep that exception scoped to documentation routes.
            content_security_policy = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://fastapi.tiangolo.com; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
            )
        else:
            content_security_policy = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'self'"
            )
        response.headers["Content-Security-Policy"] = content_security_policy
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        logger.info(
            "%s %s -> %s in %.2fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_seconds * 1_000,
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(elapsed_seconds * 1_000, 2),
            },
        )
        return response

    @application.exception_handler(IntegrityError)
    async def handle_integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        logger.info("Database constraint rejected a request: %s", exc)
        return JSONResponse(
            status_code=409,
            content={"detail": "A resource with the same unique value already exists."},
        )

    router = APIRouter(prefix=API_PREFIX)

    async def effective_scheduler_status() -> SchedulerStatusOut:
        status = await repository.get_scheduler_status() or scheduler.status()
        if status.last_error and status.last_error != "Scheduler heartbeat is stale":
            return status.model_copy(
                update={"last_error": "Scheduler cycle failed; inspect worker logs"}
            )
        return status

    @router.get("/health/live", response_model=HealthOut, tags=["System"])
    async def liveness() -> HealthOut:
        return HealthOut(status="ok", version=__version__)

    @router.get("/health/ready", response_model=ReadinessOut, tags=["System"])
    async def readiness() -> ReadinessOut:
        try:
            connected = await database.ping() and await database.schema_ready()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Database unavailable") from exc
        if not connected:
            raise HTTPException(status_code=503, detail="Database unavailable")
        try:
            scheduler_state = await effective_scheduler_status()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Database unavailable") from exc
        return ReadinessOut(
            status="ok",
            version=__version__,
            database="connected",
            scheduler=("enabled" if scheduler_state.enabled else "disabled"),
        )

    @router.get("/config", response_model=PublicConfigOut, tags=["System"])
    async def public_config() -> PublicConfigOut:
        scheduler_state = await effective_scheduler_status()
        return PublicConfigOut(
            version=__version__,
            environment=runtime_settings.environment,
            authentication_required=runtime_settings.admin_api_key is not None,
            scheduler_enabled=scheduler_state.enabled,
            webhook_configured=notifier.configured,
            retention_days=runtime_settings.retention_days,
            metrics_enabled=runtime_settings.metrics_enabled,
        )

    @router.post(
        "/services",
        response_model=ServiceOut,
        status_code=201,
        tags=["Services"],
        dependencies=[Depends(require_admin)],
    )
    async def create_service(payload: ServiceCreate, request: Request) -> ServiceOut:
        return await repository.create_service(
            payload, actor=_actor(request), request_id=_request_id(request)
        )

    @router.get("/services", response_model=list[ServiceOut], tags=["Services"])
    async def list_services(
        active_only: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[ServiceOut]:
        return await repository.list_services(
            active_only=active_only, limit=limit, offset=offset
        )

    @router.get("/services/{service_id}", response_model=ServiceOut, tags=["Services"])
    async def get_service(service_id: int) -> ServiceOut:
        service = await repository.get_service(service_id)
        if service is None:
            raise HTTPException(status_code=404, detail="Service not found")
        return service

    @router.patch(
        "/services/{service_id}",
        response_model=ServiceOut,
        tags=["Services"],
        dependencies=[Depends(require_admin)],
    )
    async def update_service(
        service_id: int, payload: ServiceUpdate, request: Request
    ) -> ServiceOut:
        service = await repository.update_service(
            service_id,
            payload,
            actor=_actor(request),
            request_id=_request_id(request),
        )
        if service is None:
            raise HTTPException(status_code=404, detail="Service not found")
        return service

    @router.delete(
        "/services/{service_id}",
        status_code=204,
        tags=["Services"],
        dependencies=[Depends(require_admin)],
    )
    async def delete_service(service_id: int, request: Request) -> Response:
        deleted = await repository.delete_service(
            service_id, actor=_actor(request), request_id=_request_id(request)
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Service not found")
        return Response(status_code=204)

    @router.post(
        "/checks/run",
        response_model=RunChecksResponse,
        tags=["Checks"],
        dependencies=[Depends(require_admin)],
    )
    async def run_checks(
        request: Request,
        payload: RunChecksRequest = Body(default_factory=RunChecksRequest),
    ) -> RunChecksResponse:
        try:
            results = await monitor.run_checks(
                service_ids=payload.service_ids,
                concurrency=payload.concurrency,
                trigger="manual",
                request_id=_request_id(request),
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
        await repository.record_audit_event(
            action="checks.manual_run",
            entity_type="check_batch",
            entity_id=None,
            actor=_actor(request),
            request_id=_request_id(request),
            metadata={"service_ids": payload.service_ids, "total": len(results)},
        )
        return RunChecksResponse(
            trigger="manual",
            total=len(results),
            healthy=healthy,
            failed=len(results) - healthy,
            results=results,
        )

    @router.get("/checks", response_model=list[CheckResultOut], tags=["Checks"])
    async def list_checks(
        service_id: int | None = Query(default=None, ge=1),
        trigger: CheckTrigger | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[CheckResultOut]:
        return await repository.list_check_results(
            service_id=service_id,
            trigger=trigger,
            limit=limit,
            offset=offset,
        )

    @router.get("/summary", response_model=SummaryOut, tags=["Checks"])
    async def summary(
        window_hours: int = Query(default=24, ge=1, le=720),
    ) -> SummaryOut:
        return await repository.build_summary(
            window_hours=window_hours,
            degraded_latency_ms=runtime_settings.degraded_latency_ms,
        )

    @router.get("/incidents", response_model=list[IncidentOut], tags=["Incidents"])
    async def list_incidents(
        status: Literal["open", "resolved"] | None = Query(default=None),
        service_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[IncidentOut]:
        return await repository.list_incidents(
            status=status, service_id=service_id, limit=limit, offset=offset
        )

    @router.get(
        "/operations/notifications",
        response_model=list[NotificationDeliveryOut],
        tags=["Operations"],
        dependencies=[Depends(require_admin)],
    )
    async def notification_history(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[NotificationDeliveryOut]:
        return await repository.list_notification_deliveries(limit=limit, offset=offset)

    @router.get(
        "/operations/audit",
        response_model=list[AuditEventOut],
        tags=["Operations"],
        dependencies=[Depends(require_admin)],
    )
    async def audit_history(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[AuditEventOut]:
        return await repository.list_audit_events(limit=limit, offset=offset)

    @router.get(
        "/operations/scheduler",
        response_model=SchedulerStatusOut,
        tags=["Operations"],
    )
    async def scheduler_status() -> SchedulerStatusOut:
        return await effective_scheduler_status()

    @router.post(
        "/operations/scheduler/run",
        response_model=SchedulerRunOut,
        tags=["Operations"],
        dependencies=[Depends(require_admin)],
    )
    async def scheduler_run(request: Request) -> SchedulerRunOut:
        executed = await scheduler.run_once()
        await repository.record_audit_event(
            action="scheduler.manual_cycle",
            entity_type="scheduler",
            entity_id=None,
            actor=_actor(request),
            request_id=_request_id(request),
            metadata={"checks_executed": executed},
        )
        return SchedulerRunOut(
            checks_executed=executed,
            status=await effective_scheduler_status(),
        )

    @router.post(
        "/operations/retention/run",
        response_model=RetentionResultOut,
        tags=["Operations"],
        dependencies=[Depends(require_admin)],
    )
    async def retention_run(request: Request) -> RetentionResultOut:
        cutoff = utc_now() - timedelta(days=runtime_settings.retention_days)
        deleted = await repository.purge_history(before=cutoff)
        await repository.record_audit_event(
            action="retention.manual_run",
            entity_type="retention",
            entity_id=None,
            actor=_actor(request),
            request_id=_request_id(request),
            metadata={"deleted": deleted, "cutoff": cutoff.isoformat()},
        )
        return RetentionResultOut(deleted_check_results=deleted, cutoff=cutoff)

    application.include_router(router)

    @application.get(
        "/api/health", response_model=ReadinessOut, include_in_schema=False
    )
    async def legacy_health() -> ReadinessOut:
        return await readiness()

    @application.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        if not runtime_settings.metrics_enabled:
            raise HTTPException(status_code=404, detail="Metrics are disabled")
        return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)

    application.mount(
        "/",
        StaticFiles(directory=STATIC_DIRECTORY, html=True),
        name="dashboard",
    )
    return application


app = create_app()
