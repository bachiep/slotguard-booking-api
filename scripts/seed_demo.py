"""Deterministic, idempotent database seed script for local evaluation and demos."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import Clock, SystemClock
from slotguard.core.config import get_settings
from slotguard.core.database import create_engine_and_sessionmaker
from slotguard.core.security import hash_password
from slotguard.models.booking import Booking, BookingStatus
from slotguard.models.resource import Resource, ResourceType
from slotguard.models.user import User, UserRole
from slotguard.models.webhook import (
    NotificationType,
    OutboxNotification,
    OutboxNotificationStatus,
    WebhookEvent,
    WebhookEventStatus,
)


@dataclass(frozen=True, slots=True)
class SeedSummary:
    """Counts of newly created records during a seed run."""

    users_created: int
    resources_created: int
    bookings_created: int


async def seed_demo_data(
    session: AsyncSession,
    *,
    clock: Clock | None = None,
) -> SeedSummary:
    """Populate deterministic demo users, resources, and sample bookings idempotently."""
    resolved_clock = clock or SystemClock()
    now = resolved_clock.now()

    users_created = 0
    resources_created = 0
    bookings_created = 0

    demo_users = [
        ("owner@slotguard.dev", "Owner@1234", "SlotGuard Venue Owner", UserRole.OWNER.value),
        ("member@slotguard.dev", "Member@1234", "Alex Member", UserRole.MEMBER.value),
        ("member2@slotguard.dev", "Member@1234", "Jordan Member", UserRole.MEMBER.value),
    ]
    user_map: dict[str, User] = {}
    for email, raw_password, full_name, role in demo_users:
        existing_user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if existing_user is None:
            existing_user = User(
                email=email,
                password_hash=hash_password(raw_password),
                full_name=full_name,
                role=role,
                is_active=True,
                created_at=now,
            )
            session.add(existing_user)
            await session.flush()
            users_created += 1
        user_map[email] = existing_user

    owner = user_map["owner@slotguard.dev"]
    member = user_map["member@slotguard.dev"]
    member_2 = user_map["member2@slotguard.dev"]

    demo_resources = [
        {
            "name": "Boardroom Alpha",
            "resource_type": ResourceType.MEETING_ROOM.value,
            "description": "12-seat executive boardroom with 4K display and whiteboard.",
            "capacity": 12,
            "hourly_rate_cents": 5000,
            "open_time": time(8, 0),
            "close_time": time(20, 0),
            "slot_duration_minutes": 60,
        },
        {
            "name": "Focus Pod B",
            "resource_type": ResourceType.WORKSPACE.value,
            "description": "Soundproof 2-person workstation pod for pair sessions.",
            "capacity": 2,
            "hourly_rate_cents": 1500,
            "open_time": time(8, 0),
            "close_time": time(20, 0),
            "slot_duration_minutes": 60,
        },
        {
            "name": "Badminton Court 1",
            "resource_type": ResourceType.SPORTS_COURT.value,
            "description": "Indoor competition badminton court with maple flooring.",
            "capacity": 4,
            "hourly_rate_cents": 2500,
            "open_time": time(7, 0),
            "close_time": time(22, 0),
            "slot_duration_minutes": 60,
        },
        {
            "name": "Podcast Studio A",
            "resource_type": ResourceType.STUDIO.value,
            "description": "Acoustically treated 4-mic podcast & livestream studio.",
            "capacity": 4,
            "hourly_rate_cents": 6000,
            "open_time": time(9, 0),
            "close_time": time(21, 0),
            "slot_duration_minutes": 60,
        },
    ]

    resource_map: dict[str, Resource] = {}
    for spec in demo_resources:
        existing_resource = (
            await session.execute(select(Resource).where(Resource.name == spec["name"]))
        ).scalar_one_or_none()
        if existing_resource is None:
            existing_resource = Resource(
                owner_id=owner.id,
                name=spec["name"],
                resource_type=spec["resource_type"],
                description=spec["description"],
                capacity=spec["capacity"],
                hourly_rate_cents=spec["hourly_rate_cents"],
                open_time=spec["open_time"],
                close_time=spec["close_time"],
                slot_duration_minutes=spec["slot_duration_minutes"],
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(existing_resource)
            await session.flush()
            resources_created += 1
        resource_map[spec["name"]] = existing_resource

    existing_booking_count = int(
        (await session.execute(select(func.count(Booking.id)))).scalar_one()
    )
    if existing_booking_count == 0:
        demo_day = now.date() + timedelta(days=1)
        boardroom = resource_map["Boardroom Alpha"]
        court = resource_map["Badminton Court 1"]

        confirmed_start = datetime.combine(demo_day, time(10, 0), tzinfo=UTC)
        confirmed_end = datetime.combine(demo_day, time(12, 0), tzinfo=UTC)
        confirmed_booking = Booking(
            resource_id=boardroom.id,
            user_id=member.id,
            start_time=confirmed_start,
            end_time=confirmed_end,
            status=BookingStatus.CONFIRMED.value,
            total_price_cents=10000,
            hold_expires_at=now + timedelta(minutes=15),
            confirmed_at=now,
            payment_reference="DEMO-PAY-001",
            notes="Quarterly sprint planning session",
            created_at=now,
            updated_at=now,
        )
        session.add(confirmed_booking)
        await session.flush()

        hold_start = datetime.combine(demo_day, time(14, 0), tzinfo=UTC)
        hold_end = datetime.combine(demo_day, time(15, 0), tzinfo=UTC)
        hold_booking = Booking(
            resource_id=court.id,
            user_id=member_2.id,
            start_time=hold_start,
            end_time=hold_end,
            status=BookingStatus.HOLD.value,
            total_price_cents=2500,
            hold_expires_at=now + timedelta(minutes=15),
            notes="Evening doubles match",
            created_at=now,
            updated_at=now,
        )
        session.add(hold_booking)

        demo_payload = json.dumps(
            {
                "provider": "demo",
                "event_id": "evt_demo_confirmed_001",
                "event_type": "payment.succeeded",
                "booking_id": confirmed_booking.id,
                "payment_reference": "DEMO-PAY-001",
                "amount_cents": 10000,
            }
        )
        session.add(
            WebhookEvent(
                provider="demo",
                event_id="evt_demo_confirmed_001",
                event_type="payment.succeeded",
                booking_id=confirmed_booking.id,
                payload_hash=hashlib.sha256(demo_payload.encode("utf-8")).hexdigest(),
                status=WebhookEventStatus.PROCESSED.value,
                received_at=now,
            )
        )
        session.add(
            OutboxNotification(
                booking_id=confirmed_booking.id,
                recipient_email=member.email,
                notification_type=NotificationType.BOOKING_CONFIRMED.value,
                payload_json=demo_payload,
                status=OutboxNotificationStatus.SENT.value,
                attempts=1,
                created_at=now,
                sent_at=now,
            )
        )
        bookings_created = 2

    await session.commit()
    return SeedSummary(
        users_created=users_created,
        resources_created=resources_created,
        bookings_created=bookings_created,
    )


async def main() -> None:
    """Run demo seeding against configured DATABASE_URL."""
    settings = get_settings()
    engine, session_maker = create_engine_and_sessionmaker(settings.database_url)
    try:
        async with session_maker() as session:
            summary = await seed_demo_data(session)
        print(
            "Seed completed: "
            f"users_created={summary.users_created}, "
            f"resources_created={summary.resources_created}, "
            f"bookings_created={summary.bookings_created}"
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
