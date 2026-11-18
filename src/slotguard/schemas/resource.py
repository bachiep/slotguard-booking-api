"""Pydantic schemas for resource management and daily slot availability."""

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slotguard.models.resource import ResourceType

ALLOWED_SLOT_DURATIONS = {30, 60, 120}


class ResourceCreateRequest(BaseModel):
    """Payload to create a new bookable resource."""

    name: str = Field(min_length=2, max_length=120)
    resource_type: ResourceType
    description: str = Field(default="", max_length=500)
    capacity: int = Field(ge=1, le=500)
    hourly_rate_cents: int = Field(ge=0, le=100_000_000)
    open_time: time = Field(default=time(8, 0))
    close_time: time = Field(default=time(20, 0))
    slot_duration_minutes: int = Field(default=60)

    @field_validator("slot_duration_minutes")
    @classmethod
    def validate_slot_duration(cls, value: int) -> int:
        if value not in ALLOWED_SLOT_DURATIONS:
            raise ValueError("slot_duration_minutes must be one of 30, 60, or 120.")
        return value

    @model_validator(mode="after")
    def validate_operating_window(self) -> "ResourceCreateRequest":
        if self.open_time.tzinfo is not None or self.close_time.tzinfo is not None:
            raise ValueError("open_time and close_time must be naive UTC wall-clock times.")
        if self.open_time >= self.close_time:
            raise ValueError("open_time must be strictly earlier than close_time.")
        return self


class ResourceUpdateRequest(BaseModel):
    """Partial update payload for an existing resource."""

    name: str | None = Field(default=None, min_length=2, max_length=120)
    resource_type: ResourceType | None = None
    description: str | None = Field(default=None, max_length=500)
    capacity: int | None = Field(default=None, ge=1, le=500)
    hourly_rate_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    open_time: time | None = None
    close_time: time | None = None
    slot_duration_minutes: int | None = None
    is_active: bool | None = None

    @field_validator("slot_duration_minutes")
    @classmethod
    def validate_slot_duration(cls, value: int | None) -> int | None:
        if value is not None and value not in ALLOWED_SLOT_DURATIONS:
            raise ValueError("slot_duration_minutes must be one of 30, 60, or 120.")
        return value


class ResourceResponse(BaseModel):
    """Public representation of a bookable resource."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    name: str
    resource_type: str
    description: str
    capacity: int
    hourly_rate_cents: int
    open_time: time
    close_time: time
    slot_duration_minutes: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PaginatedResourcesResponse(BaseModel):
    """Paginated resource collection."""

    items: list[ResourceResponse]
    total: int
    limit: int
    offset: int


class AvailabilitySlotResponse(BaseModel):
    """A discrete half-open UTC slot [start_time, end_time) with availability state."""

    start_time: datetime
    end_time: datetime
    available: bool
    status: Literal["FREE", "HOLD", "CONFIRMED", "PAST"]
    booking_id: int | None = None


class ResourceAvailabilityResponse(BaseModel):
    """Daily discrete slot availability response for a resource."""

    resource_id: int
    resource_name: str
    date: date
    slot_duration_minutes: int
    slots: list[AvailabilitySlotResponse]
