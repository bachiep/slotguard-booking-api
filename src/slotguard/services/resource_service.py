"""Business logic for resource management and daily slot availability queries."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import Clock
from slotguard.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from slotguard.models.resource import Resource
from slotguard.models.user import User
from slotguard.repositories.booking_repository import BookingRepository
from slotguard.repositories.resource_repository import ResourceRepository
from slotguard.schemas.resource import (
    AvailabilitySlotResponse,
    PaginatedResourcesResponse,
    ResourceAvailabilityResponse,
    ResourceCreateRequest,
    ResourceResponse,
    ResourceUpdateRequest,
)


class ResourceService:
    """Service managing bookable resources and computing open daily slots."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._resources = ResourceRepository(session)
        self._bookings = BookingRepository(session)

    async def create_resource(
        self,
        owner: User,
        payload: ResourceCreateRequest,
    ) -> ResourceResponse:
        """Create a new bookable resource owned by the authenticated OWNER."""
        name = payload.name.strip()
        if await self._resources.get_by_name(name) is not None:
            raise ConflictError(f"Resource '{name}' already exists.")

        now = self._clock.now()
        resource = Resource(
            owner_id=owner.id,
            name=name,
            resource_type=payload.resource_type.value,
            description=payload.description.strip(),
            capacity=payload.capacity,
            hourly_rate_cents=payload.hourly_rate_cents,
            open_time=payload.open_time,
            close_time=payload.close_time,
            slot_duration_minutes=payload.slot_duration_minutes,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        try:
            await self._resources.create(resource)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(f"Resource '{name}' already exists.") from exc
        return ResourceResponse.model_validate(resource)

    async def update_resource(
        self,
        resource_id: int,
        owner: User,
        payload: ResourceUpdateRequest,
    ) -> ResourceResponse:
        """Update an existing resource after verifying ownership and schedule invariants."""
        resource = await self._resources.get_by_id(resource_id)
        if resource is None:
            raise NotFoundError(f"Resource {resource_id} was not found.")
        if resource.owner_id != owner.id:
            raise ForbiddenError("Only the resource owner can modify this resource.")

        updates = payload.model_dump(exclude_unset=True)
        new_open = updates.get("open_time", resource.open_time)
        new_close = updates.get("close_time", resource.close_time)
        if new_open.tzinfo is not None or new_close.tzinfo is not None:
            raise BusinessRuleError("open_time and close_time must be naive UTC wall-clock times.")
        if new_open >= new_close:
            raise BusinessRuleError("open_time must be strictly earlier than close_time.")

        if "name" in updates:
            updates["name"] = updates["name"].strip()
        if "description" in updates:
            updates["description"] = updates["description"].strip()
        if "resource_type" in updates and updates["resource_type"] is not None:
            updates["resource_type"] = updates["resource_type"].value

        for field, value in updates.items():
            setattr(resource, field, value)
        resource.updated_at = self._clock.now()

        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError("Another resource with that name already exists.") from exc
        return ResourceResponse.model_validate(resource)

    async def list_resources(
        self,
        *,
        resource_type: str | None = None,
        active_only: bool = True,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResourcesResponse:
        """Return paginated resources matching optional type and active filters."""
        items, total = await self._resources.list_paginated(
            resource_type=resource_type,
            active_only=active_only,
            limit=limit,
            offset=offset,
        )
        return PaginatedResourcesResponse(
            items=[ResourceResponse.model_validate(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_resource(self, resource_id: int) -> ResourceResponse:
        """Fetch a single resource by ID or raise NotFoundError."""
        resource = await self._resources.get_by_id(resource_id)
        if resource is None:
            raise NotFoundError(f"Resource {resource_id} was not found.")
        return ResourceResponse.model_validate(resource)

    async def get_availability(
        self,
        resource_id: int,
        target_date: date,
    ) -> ResourceAvailabilityResponse:
        """Compute discrete slot availability for target_date (UTC) using indexed range scan."""
        resource = await self._resources.get_by_id(resource_id)
        if resource is None:
            raise NotFoundError(f"Resource {resource_id} was not found.")
        if not resource.is_active:
            raise BusinessRuleError("Resource is inactive and not accepting bookings.")

        day_start = datetime.combine(target_date, resource.open_time, tzinfo=UTC)
        day_end = datetime.combine(target_date, resource.close_time, tzinfo=UTC)
        active_bookings = await self._bookings.list_active_overlapping(
            resource_id=resource.id,
            window_start=day_start,
            window_end=day_end,
        )

        now = self._clock.now()
        step = timedelta(minutes=resource.slot_duration_minutes)
        slots: list[AvailabilitySlotResponse] = []
        cursor = day_start
        while cursor + step <= day_end:
            slot_end = cursor + step
            overlapping = [
                booking
                for booking in active_bookings
                if booking.start_time < slot_end and booking.end_time > cursor
            ]
            if overlapping:
                matched = overlapping[0]
                slot_status = matched.status
                matched_booking_id: int | None = matched.id
                is_available = False
            elif cursor < now:
                slot_status = "PAST"
                matched_booking_id = None
                is_available = False
            else:
                slot_status = "FREE"
                matched_booking_id = None
                is_available = True
            slots.append(
                AvailabilitySlotResponse(
                    start_time=cursor,
                    end_time=slot_end,
                    available=is_available,
                    status=slot_status,
                    booking_id=matched_booking_id,
                )
            )
            cursor = slot_end

        return ResourceAvailabilityResponse(
            resource_id=resource.id,
            resource_name=resource.name,
            date=target_date,
            slot_duration_minutes=resource.slot_duration_minutes,
            slots=slots,
        )
