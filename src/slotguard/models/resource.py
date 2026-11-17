"""Resource ORM model representing bookable spaces or equipment."""

from datetime import datetime, time
from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, Integer, String, Time
from sqlalchemy.orm import Mapped, mapped_column

from slotguard.core.database import Base, UTCDateTime


class ResourceType(StrEnum):
    """Supported categories of time-slotted resources."""

    MEETING_ROOM = "MEETING_ROOM"
    SPORTS_COURT = "SPORTS_COURT"
    STUDIO = "STUDIO"
    WORKSPACE = "WORKSPACE"


class Resource(Base):
    """A bookable resource with operating hours and discrete slot duration."""

    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    hourly_rate_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    open_time: Mapped[time] = mapped_column(Time, nullable=False)
    close_time: Mapped[time] = mapped_column(Time, nullable=False)
    slot_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
