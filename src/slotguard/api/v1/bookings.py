"""Booking creation, inspection, and cancellation endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.api.deps import (
    get_app_clock,
    get_app_settings,
    get_current_user,
    get_metrics,
    get_resource_locks,
    rate_limit,
)
from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.database import get_db_session
from slotguard.core.locks import ResourceLockManager
from slotguard.core.metrics import SlotGuardMetrics
from slotguard.core.problem_details import ProblemDetail
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
    metrics: SlotGuardMetrics,
) -> BookingService:
    return BookingService(
        session=session,
        settings=settings,
        clock=clock,
        lock_manager=lock_manager,
        metrics=metrics,
    )


@router.post(
    "",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Reserve a time slot (creates 15-minute HOLD)",
    dependencies=[
        Depends(rate_limit("bookings_create", limit_getter=lambda s: s.rate_limit_booking_requests))
    ],
    responses={
        401: {"model": ProblemDetail, "description": "Missing or invalid Bearer token"},
        404: {"model": ProblemDetail, "description": "Resource not found"},
        409: {"model": ProblemDetail, "description": "Time slot overlaps existing active booking"},
        422: {"model": ProblemDetail, "description": "Invalid slot interval or business rule"},
        429: {"model": ProblemDetail, "description": "Booking rate limit exceeded"},
    },
)
async def create_booking(
    payload: BookingCreateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
    lock_manager: Annotated[ResourceLockManager, Depends(get_resource_locks)],
    metrics: Annotated[SlotGuardMetrics, Depends(get_metrics)],
) -> BookingResponse:
    """Create a HOLD booking after serialized half-open interval overlap verification."""
    return await _build_booking_service(
        session, settings, clock, lock_manager, metrics
    ).create_booking(
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
    metrics: Annotated[SlotGuardMetrics, Depends(get_metrics)],
    booking_status: BookingStatus | None = Query(default=None, alias="status"),
    mine: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedBookingsResponse:
    """Return paginated bookings (MEMBER sees own bookings; OWNER sees all unless mine=true)."""
    return await _build_booking_service(
        session, settings, clock, lock_manager, metrics
    ).list_bookings(
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
    metrics: Annotated[SlotGuardMetrics, Depends(get_metrics)],
) -> BookingResponse:
    """Fetch a booking by ID (booking creator or OWNER only)."""
    return await _build_booking_service(
        session, settings, clock, lock_manager, metrics
    ).get_booking(booking_id, current_user)


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
    metrics: Annotated[SlotGuardMetrics, Depends(get_metrics)],
) -> BookingResponse:
    """Cancel a HOLD or CONFIRMED booking before its start_time, freeing the slot."""
    return await _build_booking_service(
        session, settings, clock, lock_manager, metrics
    ).cancel_booking(booking_id, current_user)
