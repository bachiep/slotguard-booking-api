"""Shared FastAPI dependencies for configuration, auth, locks, and RBAC."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.database import get_db_session
from slotguard.core.exceptions import ForbiddenError, UnauthorizedError
from slotguard.core.locks import ResourceLockManager
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
