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


class WebhookDemoSignRequest(BaseModel):
    """Non-production request payload to sign a payment webhook server-side."""

    booking_id: int = Field(ge=1)
    event_id: str | None = Field(default=None, min_length=3, max_length=120)
    provider: str = Field(default="payos", min_length=2, max_length=50)
    event_type: str = Field(default="payment.succeeded", min_length=3, max_length=80)
    payment_reference: str | None = Field(default=None, min_length=2, max_length=120)
    amount_cents: int | None = Field(default=None, ge=0)


class WebhookDemoSignResponse(BaseModel):
    """Signed webhook headers and serialized payload bytes for non-prod testing."""

    timestamp: str
    signature: str
    raw_body: str
    event_id: str
    booking_id: int
    provider: str
    amount_cents: int
