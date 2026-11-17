"""Async repository for Booking queries backed by composite window indexes."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.models.booking import Booking, BookingStatus

ACTIVE_BOOKING_STATUSES = (BookingStatus.HOLD.value, BookingStatus.CONFIRMED.value)


class BookingRepository:
    """Data access helpers for time-slot bookings."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active_overlapping(
        self,
        *,
        resource_id: int,
        window_start: datetime,
        window_end: datetime,
    ) -> list[Booking]:
        """Query HOLD/CONFIRMED bookings overlapping [window_start, window_end)."""
        stmt = (
            select(Booking)
            .where(
                Booking.resource_id == resource_id,
                Booking.status.in_(ACTIVE_BOOKING_STATUSES),
                Booking.start_time < window_end,
                Booking.end_time > window_start,
            )
            .order_by(Booking.start_time.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())
