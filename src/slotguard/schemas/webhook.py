"""Pydantic schemas for payment webhook payloads and acknowledgments."""

from pydantic import BaseModel, Field


class PaymentWebhookPayload(BaseModel):
    """Inbound payment gateway event payload."""

    provider: str = Field(min_length=2, max_length=50)
    event_id: str = Field(min_length=3, max_length=120)
    event_type: str = Field(min_length=3, max_length=80)
    booking_id: int = Field(ge=1)
    payment_reference: str = Field(min_length=2, max_length=120)
    amount_cents: int = Field(ge=0)


class WebhookAckResponse(BaseModel):
    """Idempotent webhook processing acknowledgment."""

    status: str
    event_id: str
    booking_id: int
    booking_status: str | None = None
