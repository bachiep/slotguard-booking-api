"""Booking ORM model with composite availability and hold-expiry indexes."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from slotguard.core.database import Base, UTCDateTime


class BookingStatus(StrEnum):
    """Lifecycle states of a time-slot reservation."""

    HOLD = "HOLD"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class Booking(Base):
    """Time-slot reservation using half-open [start_time, end_time) UTC intervals."""

    __tablename__ = "bookings"
    __table_args__ = (
        Index(
            "ix_bookings_resource_status_window",
            "resource_id",
            "status",
            "start_time",
            "end_time",
        ),
        Index(
            "ix_bookings_status_hold_expires_at",
            "status",
            "hold_expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    start_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BookingStatus.HOLD.value,
    )
    total_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    hold_expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
