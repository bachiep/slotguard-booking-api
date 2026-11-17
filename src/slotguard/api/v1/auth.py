"""Authentication and user profile endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.api.deps import get_app_clock, get_app_settings, get_current_user, rate_limit
from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.database import get_db_session
from slotguard.models.user import User
from slotguard.schemas.auth import (
    AuthTokenResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    UserResponse,
)
from slotguard.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _build_auth_service(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
) -> AuthService:
    return AuthService(session=session, settings=settings, clock=clock)


@router.post(
    "/register",
    response_model=AuthTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a member account",
)
async def register(
    payload: RegisterRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
) -> AuthTokenResponse:
    """Create a new MEMBER user account and issue access and refresh tokens."""
    return await _build_auth_service(session, settings, clock).register(payload)


@router.post(
    "/login",
    response_model=AuthTokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate user credentials",
    dependencies=[
        Depends(rate_limit("auth_login", limit_getter=lambda s: s.rate_limit_auth_requests))
    ],
)
async def login(
    payload: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
) -> AuthTokenResponse:
    """Verify email/password and return access and refresh tokens."""
    return await _build_auth_service(session, settings, clock).login(payload)


@router.post(
    "/refresh",
    response_model=AuthTokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Rotate refresh token",
)
async def refresh(
    payload: RefreshRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
) -> AuthTokenResponse:
    """Rotate refresh token and revoke the entire token family if reuse is detected."""
    return await _build_auth_service(session, settings, clock).refresh(payload.refresh_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke refresh token family",
)
async def logout(
    payload: RefreshRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
) -> Response:
    """Revoke the refresh token family associated with the supplied token."""
    await _build_auth_service(session, settings, clock).logout(payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get authenticated user profile",
)
async def read_current_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserResponse:
    """Return profile details for the currently authenticated caller."""
    return UserResponse.model_validate(current_user)
