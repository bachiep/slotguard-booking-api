"""Async repository for Booking persistence and half-open interval overlap checks."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.models.booking import Booking, BookingStatus
from slotguard.models.resource import Resource

ACTIVE_BOOKING_STATUSES = (BookingStatus.HOLD.value, BookingStatus.CONFIRMED.value)


class BookingRepository:
    """Data access helpers for time-slot bookings."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, booking_id: int) -> Booking | None:
        result = await self._session.execute(select(Booking).where(Booking.id == booking_id))
        return result.scalar_one_or_none()

    async def lock_resource_for_booking(self, resource_id: int) -> Resource | None:
        """Fetch target resource, acquiring FOR UPDATE row lock on PostgreSQL."""
        stmt = select(Resource).where(Resource.id == resource_id)
        bind = self._session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, booking: Booking) -> Booking:
        self._session.add(booking)
        await self._session.flush()
        return booking

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

    async def has_active_overlap(
        self,
        *,
        resource_id: int,
        start_time: datetime,
        end_time: datetime,
        exclude_booking_id: int | None = None,
    ) -> bool:
        """Check whether any HOLD/CONFIRMED booking overlaps half-open [start_time, end_time)."""
        stmt = select(Booking.id).where(
            Booking.resource_id == resource_id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            Booking.start_time < end_time,
            Booking.end_time > start_time,
        )
        if exclude_booking_id is not None:
            stmt = stmt.where(Booking.id != exclude_booking_id)
        stmt = stmt.limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def list_paginated(
        self,
        *,
        user_id: int | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Booking], int]:
        filters = []
        if user_id is not None:
            filters.append(Booking.user_id == user_id)
        if status is not None:
            filters.append(Booking.status == status)

        total = int(
            (
                await self._session.execute(select(func.count(Booking.id)).where(*filters))
            ).scalar_one()
        )
        stmt = (
            select(Booking)
            .where(*filters)
            .order_by(Booking.start_time.asc(), Booking.id.asc())
            .limit(limit)
            .offset(offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return items, total

    async def count_by_status(self) -> dict[str, int]:
        """Return persisted booking counts grouped by status."""
        stmt = select(Booking.status, func.count(Booking.id)).group_by(Booking.status)
        rows = (await self._session.execute(stmt)).all()
        return {str(status_value): int(count) for status_value, count in rows}
