"""Booking reservation, overlap validation, concurrency control, and cancellation."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import Clock, ensure_utc
from slotguard.core.config import Settings
from slotguard.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from slotguard.core.locks import ResourceLockManager
from slotguard.models.booking import Booking, BookingStatus
from slotguard.models.resource import Resource
from slotguard.models.user import User, UserRole
from slotguard.repositories.booking_repository import BookingRepository
from slotguard.schemas.booking import (
    BookingCreateRequest,
    BookingResponse,
    PaginatedBookingsResponse,
)

CANCELLABLE_STATUSES = {BookingStatus.HOLD.value, BookingStatus.CONFIRMED.value}


class BookingService:
    """Service orchestrating time-slot reservations and lifecycle transitions."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        clock: Clock,
        lock_manager: ResourceLockManager,
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._lock_manager = lock_manager
        self._bookings = BookingRepository(session)

    @staticmethod
    def _validate_slot_against_resource(
        resource: Resource,
        start_time: datetime,
        end_time: datetime,
        now: datetime,
    ) -> int:
        if not resource.is_active:
            raise BusinessRuleError("Resource is inactive and not accepting bookings.")
        if start_time < now:
            raise BusinessRuleError("Cannot create a booking starting in the past.")
        if start_time.date() != end_time.date():
            raise BusinessRuleError("Bookings must start and end within the same UTC calendar day.")

        day = start_time.date()
        open_dt = datetime.combine(day, resource.open_time, tzinfo=UTC)
        close_dt = datetime.combine(day, resource.close_time, tzinfo=UTC)
        if start_time < open_dt or end_time > close_dt:
            raise BusinessRuleError(
                f"Booking interval must fall within operating hours "
                f"{resource.open_time.isoformat()}-{resource.close_time.isoformat()} UTC."
            )

        offset_seconds = int((start_time - open_dt).total_seconds())
        duration_seconds = int((end_time - start_time).total_seconds())
        slot_seconds = resource.slot_duration_minutes * 60
        if offset_seconds % slot_seconds != 0 or duration_seconds % slot_seconds != 0:
            raise BusinessRuleError(
                f"Booking times must align with {resource.slot_duration_minutes}-minute slots."
            )
        return duration_seconds // 60

    async def create_booking(
        self,
        user_id: int,
        payload: BookingCreateRequest,
    ) -> BookingResponse:
        """Reserve a time slot as a HOLD with serialized half-open overlap prevention."""
        start_time = ensure_utc(payload.start_time)
        end_time = ensure_utc(payload.end_time)

        async with self._lock_manager.acquire(payload.resource_id):
            # End any read transaction opened by auth dependencies so the overlap check
            # sees the latest committed bookings from preceding serialized requests.
            await self._session.rollback()

            now = self._clock.now()
            resource = await self._bookings.lock_resource_for_booking(payload.resource_id)
            if resource is None:
                raise NotFoundError(f"Resource {payload.resource_id} was not found.")

            duration_minutes = self._validate_slot_against_resource(
                resource, start_time, end_time, now
            )

            if await self._bookings.has_active_overlap(
                resource_id=resource.id,
                start_time=start_time,
                end_time=end_time,
            ):
                raise ConflictError("Requested time slot overlaps an existing active booking.")

            total_price_cents = (resource.hourly_rate_cents * duration_minutes) // 60
            booking = Booking(
                resource_id=resource.id,
                user_id=user_id,
                start_time=start_time,
                end_time=end_time,
                status=BookingStatus.HOLD.value,
                total_price_cents=total_price_cents,
                hold_expires_at=now + timedelta(seconds=self._settings.booking_hold_ttl_seconds),
                notes=payload.notes.strip() if payload.notes else None,
                created_at=now,
                updated_at=now,
            )
            await self._bookings.create(booking)
            await self._session.commit()
            return BookingResponse.model_validate(booking)

    async def list_bookings(
        self,
        caller: User,
        *,
        status: BookingStatus | None = None,
        mine_only: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedBookingsResponse:
        """List bookings scoped to the caller (or all bookings for OWNER)."""
        scoped_user_id = caller.id if (mine_only or caller.role != UserRole.OWNER.value) else None
        items, total = await self._bookings.list_paginated(
            user_id=scoped_user_id,
            status=status.value if status is not None else None,
            limit=limit,
            offset=offset,
        )
        return PaginatedBookingsResponse(
            items=[BookingResponse.model_validate(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_booking(self, booking_id: int, caller: User) -> BookingResponse:
        """Retrieve a booking if the caller owns it or holds the OWNER role."""
        booking = await self._bookings.get_by_id(booking_id)
        if booking is None:
            raise NotFoundError(f"Booking {booking_id} was not found.")
        if booking.user_id != caller.id and caller.role != UserRole.OWNER.value:
            raise ForbiddenError("You are not authorized to access this booking.")
        return BookingResponse.model_validate(booking)

    async def cancel_booking(self, booking_id: int, caller: User) -> BookingResponse:
        """Cancel an active HOLD or CONFIRMED booking prior to its start_time."""
        booking = await self._bookings.get_by_id(booking_id)
        if booking is None:
            raise NotFoundError(f"Booking {booking_id} was not found.")
        if booking.user_id != caller.id and caller.role != UserRole.OWNER.value:
            raise ForbiddenError("You are not authorized to cancel this booking.")

        async with self._lock_manager.acquire(booking.resource_id):
            await self._session.rollback()
            fresh = await self._bookings.get_by_id(booking_id)
            if fresh is None:
                raise NotFoundError(f"Booking {booking_id} was not found.")
            if fresh.status not in CANCELLABLE_STATUSES:
                raise ConflictError(f"Cannot cancel a booking in '{fresh.status}' status.")
            now = self._clock.now()
            if fresh.start_time <= now:
                raise ConflictError("Cannot cancel a booking at or after its start_time.")

            fresh.status = BookingStatus.CANCELLED.value
            fresh.cancelled_at = now
            fresh.updated_at = now
            await self._session.commit()
            return BookingResponse.model_validate(fresh)
