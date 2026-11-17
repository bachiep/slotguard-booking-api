"""WebhookEvent and OutboxNotification ORM models."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from slotguard.core.database import Base, UTCDateTime


class WebhookEventStatus(StrEnum):
    """Processing outcomes for persisted gateway webhook events."""

    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"


class OutboxNotificationStatus(StrEnum):
    """Delivery states for transactional outbox notifications."""

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class NotificationType(StrEnum):
    """Domain notification event types."""

    BOOKING_CONFIRMED = "BOOKING_CONFIRMED"
    BOOKING_EXPIRED = "BOOKING_EXPIRED"


class WebhookEvent(Base):
    """Idempotency ledger deduplicating external payment callbacks by (provider, event_id)."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_webhook_events_provider_event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    event_id: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True,
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class OutboxNotification(Base):
    """Transactional outbox message queued atomically with booking state transitions."""

    __tablename__ = "outbox_notifications"
    __table_args__ = (Index("ix_outbox_notifications_status_created_at", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"),
        nullable=False,
    )
    recipient_email: Mapped[str] = mapped_column(String(255), nullable=False)
    notification_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=OutboxNotificationStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
