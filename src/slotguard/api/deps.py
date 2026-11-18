"""Shared FastAPI dependencies for configuration, auth, locks, rate limits, and RBAC."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.database import get_db_session
from slotguard.core.exceptions import (
    ForbiddenError,
    RateLimitExceededError,
    UnauthorizedError,
)
from slotguard.core.locks import ResourceLockManager
from slotguard.core.metrics import SlotGuardMetrics
from slotguard.core.rate_limiter import RateLimiter
from slotguard.core.security import decode_access_token
from slotguard.models.user import User, UserRole
from slotguard.repositories.user_repository import UserRepository

bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    bearerFormat="JWT",
    description="Enter JWT access token obtained from /api/v1/auth/login",
)


def get_app_settings(request: Request) -> Settings:
    """Retrieve application Settings from FastAPI state."""
    return request.app.state.settings


def get_app_clock(request: Request) -> Clock:
    """Retrieve injectable Clock from FastAPI state."""
    return request.app.state.clock


def get_resource_locks(request: Request) -> ResourceLockManager:
    """Retrieve app-scoped ResourceLockManager from FastAPI state."""
    return request.app.state.resource_locks


def get_rate_limiter(request: Request) -> RateLimiter:
    """Retrieve sliding-window RateLimiter from FastAPI state."""
    return request.app.state.rate_limiter


def get_metrics(request: Request) -> SlotGuardMetrics:
    """Retrieve app-scoped SlotGuardMetrics collector from FastAPI state."""
    return request.app.state.metrics


def rate_limit(
    scope: str,
    *,
    limit_getter: Callable[[Settings], int],
) -> Callable[..., Awaitable[None]]:
    """Build a FastAPI dependency enforcing a sliding-window rate limit per client IP."""

    async def _enforce(
        request: Request,
        response: Response,
        settings: Annotated[Settings, Depends(get_app_settings)],
        limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
        metrics: Annotated[SlotGuardMetrics, Depends(get_metrics)],
    ) -> None:
        client_host = request.client.host if request.client is not None else "unknown"
        limit = limit_getter(settings)
        result = await limiter.check(
            key=f"{scope}:{client_host}",
            limit=limit,
            window_seconds=settings.rate_limit_window_seconds,
        )
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        if not result.allowed:
            metrics.record_rate_limit_hit()
            raise RateLimitExceededError(
                limit=result.limit,
                remaining=result.remaining,
                retry_after_seconds=result.retry_after_seconds,
            )

    return _enforce


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
) -> User:
    """Validate Bearer JWT and return the active authenticated User."""
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise UnauthorizedError("Missing or malformed Bearer token.")

    claims = decode_access_token(
        credentials.credentials,
        settings=settings,
        clock=clock,
    )
    user = await UserRepository(session).get_by_id(claims.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("Authenticated user does not exist or is inactive.")
    return user


def require_roles(*allowed_roles: UserRole) -> Callable[..., User]:
    """Build a FastAPI dependency requiring the caller to hold one of allowed_roles."""
    allowed = {role.value for role in allowed_roles}

    async def _role_guard(current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if current_user.role not in allowed:
            raise ForbiddenError("Insufficient permissions for this operation.")
        return current_user

    return _role_guard
