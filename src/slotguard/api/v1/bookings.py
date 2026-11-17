"""Booking creation, inspection, and cancellation endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.api.deps import (
    get_app_clock,
    get_app_settings,
    get_current_user,
    get_resource_locks,
    rate_limit,
)
from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.database import get_db_session
from slotguard.core.locks import ResourceLockManager
from slotguard.models.booking import BookingStatus
from slotguard.models.user import User
from slotguard.schemas.booking import (
    BookingCreateRequest,
    BookingResponse,
    PaginatedBookingsResponse,
)
from slotguard.services.booking_service import BookingService

router = APIRouter(prefix="/bookings", tags=["Bookings"])


def _build_booking_service(
    session: AsyncSession,
    settings: Settings,
    clock: Clock,
    lock_manager: ResourceLockManager,
) -> BookingService:
    return BookingService(
        session=session,
        settings=settings,
        clock=clock,
        lock_manager=lock_manager,
    )


@router.post(
    "",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Reserve a time slot (creates 15-minute HOLD)",
    dependencies=[
        Depends(rate_limit("bookings_create", limit_getter=lambda s: s.rate_limit_booking_requests))
    ],
)
async def create_booking(
    payload: BookingCreateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
    lock_manager: Annotated[ResourceLockManager, Depends(get_resource_locks)],
) -> BookingResponse:
    """Create a HOLD booking after serialized half-open interval overlap verification."""
    return await _build_booking_service(session, settings, clock, lock_manager).create_booking(
        current_user.id,
        payload,
    )


@router.get(
    "",
    response_model=PaginatedBookingsResponse,
    summary="List bookings",
)
async def list_bookings(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
    lock_manager: Annotated[ResourceLockManager, Depends(get_resource_locks)],
    booking_status: BookingStatus | None = Query(default=None, alias="status"),
    mine: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedBookingsResponse:
    """Return paginated bookings (MEMBER sees own bookings; OWNER sees all unless mine=true)."""
    return await _build_booking_service(session, settings, clock, lock_manager).list_bookings(
        current_user,
        status=booking_status,
        mine_only=mine,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{booking_id}",
    response_model=BookingResponse,
    summary="Get booking details",
)
async def get_booking(
    booking_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
    lock_manager: Annotated[ResourceLockManager, Depends(get_resource_locks)],
) -> BookingResponse:
    """Fetch a booking by ID (booking creator or OWNER only)."""
    return await _build_booking_service(session, settings, clock, lock_manager).get_booking(
        booking_id, current_user
    )


@router.post(
    "/{booking_id}/cancel",
    response_model=BookingResponse,
    summary="Cancel an active booking",
)
async def cancel_booking(
    booking_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
    lock_manager: Annotated[ResourceLockManager, Depends(get_resource_locks)],
) -> BookingResponse:
    """Cancel a HOLD or CONFIRMED booking before its start_time, freeing the slot."""
    return await _build_booking_service(session, settings, clock, lock_manager).cancel_booking(
        booking_id, current_user
    )
