"""Pydantic schemas for observability telemetry and transactional outbox inspection."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class RequestMetricsSummary(BaseModel):
    """HTTP request and rate-limit counter summary."""

    total: int
    status_2xx: int
    status_4xx: int
    status_5xx: int
    rate_limit_hits_429: int


class LatencyMetricsSummary(BaseModel):
    """Rolling request latency percentiles in milliseconds."""

    sample_count: int
    p50: float
    p95: float
    avg: float


class BookingMetricsSummary(BaseModel):
    """Booking creation, conflict blocking, and cancellation counters."""

    holds_created: int
    conflicts_blocked_409: int
    cancellations: int


class WebhookMetricsSummary(BaseModel):
    """HMAC webhook verification, replay, and rejection counters."""

    verified: int
    processed: int
    idempotent_replays: int
    rejected: int
    ignored: int


class MaintenanceMetricsSummary(BaseModel):
    """Hold-expiry sweep and outbox dispatch counters."""

    expired_holds: int
    outbox_processed: int
    outbox_failed: int


class DatabaseTotalsSummary(BaseModel):
    """Live persisted entity counts grouped by lifecycle status."""

    bookings_hold: int
    bookings_confirmed: int
    bookings_cancelled: int
    bookings_expired: int
    outbox_pending: int
    outbox_sent: int
    outbox_failed: int


class MetricsSummaryResponse(BaseModel):
    """Structured runtime telemetry and inventory snapshot."""

    service: str
    version: str
    environment: str
    uptime_seconds: float
    timestamp: datetime
    requests: RequestMetricsSummary
    latency_ms: LatencyMetricsSummary
    bookings: BookingMetricsSummary
    webhooks: WebhookMetricsSummary
    maintenance: MaintenanceMetricsSummary
    database_totals: DatabaseTotalsSummary


class OutboxRecentItem(BaseModel):
    """Sanitized representation of a transactional outbox notification row."""

    id: int
    booking_id: int
    event_type: str
    status: str
    attempts: int
    created_at: datetime
    sent_at: datetime | None
    payload_summary: dict[str, Any]


class OutboxRecentResponse(BaseModel):
    """Recent outbox notification feed."""

    items: list[OutboxRecentItem]
    total: int


class MaintenanceRunResponse(BaseModel):
    """Result of an on-demand maintenance worker sweep."""

    expired_holds: int
    outbox_sent: int
    outbox_failed: int
