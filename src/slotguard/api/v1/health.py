"""Health probe endpoint."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.database import get_db_session

router = APIRouter(tags=["Health"])


class HealthResponse(BaseModel):
    """Health check payload reporting API and database readiness."""

    status: str
    service: str
    environment: str
    database_dialect: str
    rate_limit_backend: str
    timestamp: datetime


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health check",
    description="Executes a lightweight database connectivity check and returns runtime status.",
)
async def health_check(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> HealthResponse:
    """Verify database connectivity and return service readiness metadata."""
    await session.execute(text("SELECT 1"))
    settings: Settings = request.app.state.settings
    clock: Clock = request.app.state.clock
    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else "unknown"
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.app_env,
        database_dialect=dialect,
        rate_limit_backend=settings.rate_limit_backend,
        timestamp=clock.now(),
    )
