"""FastAPI application factory, OpenAPI Bearer security setup, and lifespan management."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from slotguard import __version__
from slotguard.api.v1.router import api_v1_router
from slotguard.core.clock import Clock, SystemClock
from slotguard.core.config import Settings, get_settings
from slotguard.core.database import create_engine_and_sessionmaker
from slotguard.core.locks import ResourceLockManager
from slotguard.core.metrics import SlotGuardMetrics
from slotguard.core.middleware import RequestContextMiddleware, configure_structured_logging
from slotguard.core.problem_details import register_exception_handlers
from slotguard.core.rate_limiter import RateLimiter, build_rate_limiter
from slotguard.workers.maintenance_worker import MaintenanceWorker

_CONSOLE_HTML_PATH = Path(__file__).resolve().parent / "static" / "index.html"


def _configure_openapi(app: FastAPI) -> None:
    """Attach HTTP Bearer authentication scheme to OpenAPI specification."""

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Enter JWT access token obtained from /api/v1/auth/login",
        }
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    engine: AsyncEngine | None = None,
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    rate_limiter: RateLimiter | None = None,
    metrics: SlotGuardMetrics | None = None,
) -> FastAPI:
    """Create and configure a FastAPI application instance."""
    resolved_settings = settings or get_settings()
    configure_structured_logging(resolved_settings.log_level)
    resolved_clock = clock or SystemClock()
    lock_manager = ResourceLockManager()
    resolved_limiter = rate_limiter or build_rate_limiter(resolved_settings, resolved_clock)
    resolved_metrics = metrics or SlotGuardMetrics(clock=resolved_clock)

    owns_engine = engine is None or session_maker is None
    if engine is None or session_maker is None:
        created_engine, created_session_maker = create_engine_and_sessionmaker(
            resolved_settings.database_url
        )
        resolved_engine = created_engine
        resolved_session_maker = created_session_maker
    else:
        resolved_engine = engine
        resolved_session_maker = session_maker

    worker: MaintenanceWorker | None = None
    if resolved_settings.worker_enabled:
        worker = MaintenanceWorker(
            session_maker=resolved_session_maker,
            clock=resolved_clock,
            lock_manager=lock_manager,
            poll_interval_seconds=resolved_settings.worker_poll_interval_seconds,
            metrics=resolved_metrics,
        )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = resolved_settings
        application.state.clock = resolved_clock
        application.state.engine = resolved_engine
        application.state.session_maker = resolved_session_maker
        application.state.resource_locks = lock_manager
        application.state.rate_limiter = resolved_limiter
        application.state.metrics = resolved_metrics
        if worker is not None:
            worker.start()
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop()
            await resolved_limiter.close()
            if owns_engine:
                await resolved_engine.dispose()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        description=(
            "Async resource time-slot booking REST API with double-booking prevention, "
            "HMAC-SHA256 idempotent payment webhooks, sliding-window rate limiting, "
            "and real-time observability."
        ),
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.clock = resolved_clock
    application.state.engine = resolved_engine
    application.state.session_maker = resolved_session_maker
    application.state.resource_locks = lock_manager
    application.state.rate_limiter = resolved_limiter
    application.state.metrics = resolved_metrics

    application.add_middleware(RequestContextMiddleware, metrics=resolved_metrics)
    register_exception_handlers(application, resolved_clock)

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def serve_console() -> HTMLResponse:
        """Serve the SlotGuard Timeline & Concurrency Lab web console."""
        return HTMLResponse(content=_CONSOLE_HTML_PATH.read_text(encoding="utf-8"))

    application.include_router(api_v1_router)
    _configure_openapi(application)
    return application


app = create_app()
