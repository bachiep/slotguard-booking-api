"""Pydantic schemas for time-slot booking creation, listing, and cancellation."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BookingCreateRequest(BaseModel):
    """Payload to reserve a half-open [start_time, end_time) UTC time slot."""

    resource_id: int = Field(ge=1)
    start_time: datetime
    end_time: datetime
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_interval(self) -> "BookingCreateRequest":
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("start_time and end_time must include explicit UTC timezone offsets.")
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be strictly earlier than end_time.")
        return self


class BookingRescheduleRequest(BaseModel):
    """Payload to reschedule an active booking to a new [start_time, end_time) UTC slot."""

    start_time: datetime
    end_time: datetime

    @model_validator(mode="after")
    def validate_interval(self) -> "BookingRescheduleRequest":
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("start_time and end_time must include explicit UTC timezone offsets.")
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be strictly earlier than end_time.")
        return self


class BookingResponse(BaseModel):
    """Representation of a booking reservation."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    resource_id: int
    user_id: int
    start_time: datetime
    end_time: datetime
    status: str
    total_price_cents: int
    hold_expires_at: datetime
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    payment_reference: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class PaginatedBookingsResponse(BaseModel):
    """Paginated booking list payload."""

    items: list[BookingResponse]
    total: int
    limit: int
    offset: int
