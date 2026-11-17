"""Sliding-window rate limiter and hold-expiry / outbox worker tests."""

import json

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from slotguard.core.clock import MutableClock
from slotguard.core.config import Settings
from slotguard.core.locks import ResourceLockManager
from slotguard.core.rate_limiter import InMemorySlidingWindowLimiter
from slotguard.core.webhook_security import compute_webhook_signature
from slotguard.models.booking import Booking
from slotguard.models.user import UserRole
from slotguard.models.webhook import OutboxNotification
from slotguard.services.maintenance_service import MaintenanceService
from slotguard.workers.maintenance_worker import MaintenanceWorker


async def test_in_memory_sliding_window_enforces_quota_and_recovers(
    clock: MutableClock,
) -> None:
    limiter = InMemorySlidingWindowLimiter(clock=clock)

    r1 = await limiter.check("login:127.0.0.1", limit=3, window_seconds=60)
    r2 = await limiter.check("login:127.0.0.1", limit=3, window_seconds=60)
    r3 = await limiter.check("login:127.0.0.1", limit=3, window_seconds=60)
    r4 = await limiter.check("login:127.0.0.1", limit=3, window_seconds=60)

    assert (r1.allowed, r1.remaining) == (True, 2)
    assert (r2.allowed, r2.remaining) == (True, 1)
    assert (r3.allowed, r3.remaining) == (True, 0)
    assert (r4.allowed, r4.remaining, r4.retry_after_seconds) == (False, 0, 60)

    clock.advance(seconds=30)
    r5 = await limiter.check("login:127.0.0.1", limit=3, window_seconds=60)
    assert (r5.allowed, r5.retry_after_seconds) == (False, 30)

    clock.advance(seconds=31)
    r6 = await limiter.check("login:127.0.0.1", limit=3, window_seconds=60)
    assert (r6.allowed, r6.remaining) == (True, 2)


async def test_login_endpoint_returns_429_with_rate_limit_headers(
    app,
    client: AsyncClient,
    clock: MutableClock,
    test_settings: Settings,
    user_factory,
) -> None:
    test_settings.rate_limit_auth_requests = 2
    test_settings.rate_limit_window_seconds = 60
    await user_factory("ratelimit@example.com", password="Password123!")

    login_payload = {"email": "ratelimit@example.com", "password": "Password123!"}

    first = await client.post("/api/v1/auth/login", json=login_payload)
    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "2"
    assert first.headers["X-RateLimit-Remaining"] == "1"

    second = await client.post("/api/v1/auth/login", json=login_payload)
    assert second.status_code == 200
    assert second.headers["X-RateLimit-Remaining"] == "0"

    blocked = await client.post("/api/v1/auth/login", json=login_payload)
    assert blocked.status_code == 429
    assert blocked.headers["content-type"].startswith("application/problem+json")
    assert blocked.headers["Retry-After"] == "60"
    assert blocked.headers["X-RateLimit-Limit"] == "2"
    assert blocked.headers["X-RateLimit-Remaining"] == "0"

    clock.advance(seconds=61)
    recovered = await client.post("/api/v1/auth/login", json=login_payload)
    assert recovered.status_code == 200


async def test_hold_expiry_worker_releases_slot_and_dispatches_outbox(
    app,
    client: AsyncClient,
    clock: MutableClock,
    db_engine_and_session_maker: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    db_session: AsyncSession,
    user_factory,
    auth_headers_factory,
) -> None:
    _, session_maker = db_engine_and_session_maker
    owner = await user_factory("owner-exp@example.com", role=UserRole.OWNER)
    member_1 = await user_factory("member1-exp@example.com", role=UserRole.MEMBER)
    member_2 = await user_factory("member2-exp@example.com", role=UserRole.MEMBER)

    res_resp = await client.post(
        "/api/v1/resources",
        json={
            "name": "Focus Pod 1",
            "resource_type": "WORKSPACE",
            "capacity": 1,
            "hourly_rate_cents": 1500,
            "open_time": "08:00:00",
            "close_time": "20:00:00",
            "slot_duration_minutes": 60,
        },
        headers=auth_headers_factory(owner),
    )
    resource_id = res_resp.json()["id"]

    hold_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T12:00:00Z",
            "end_time": "2026-12-01T13:00:00Z",
        },
        headers=auth_headers_factory(member_1),
    )
    assert hold_resp.status_code == 201
    booking_id = hold_resp.json()["id"]

    # Contending booking is blocked while HOLD is active
    blocked = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T12:00:00Z",
            "end_time": "2026-12-01T13:00:00Z",
        },
        headers=auth_headers_factory(member_2),
    )
    assert blocked.status_code == 409

    # Advance past 15-minute hold TTL and run maintenance worker cycle
    clock.advance(minutes=16)
    worker = MaintenanceWorker(
        session_maker=session_maker,
        clock=clock,
        lock_manager=app.state.resource_locks,
    )
    expired_count, sent_count = await worker.run_once()
    assert expired_count == 1
    assert sent_count == 1

    expired_booking = (
        await db_session.execute(select(Booking).where(Booking.id == booking_id))
    ).scalar_one()
    assert expired_booking.status == "EXPIRED"

    notices = (await db_session.execute(select(OutboxNotification))).scalars().all()
    assert len(notices) == 1
    assert notices[0].notification_type == "BOOKING_EXPIRED"
    assert notices[0].status == "SENT"

    # Slot is now open in availability and re-bookable
    avail_resp = await client.get(
        f"/api/v1/resources/{resource_id}/availability",
        params={"date": "2026-12-01"},
    )
    slot_12 = next(
        s for s in avail_resp.json()["slots"] if s["start_time"].startswith("2026-12-01T12:00:00")
    )
    assert slot_12["available"] is True

    rebook_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T12:00:00Z",
            "end_time": "2026-12-01T13:00:00Z",
        },
        headers=auth_headers_factory(member_2),
    )
    assert rebook_resp.status_code == 201


async def test_outbox_worker_handles_delivery_and_retry_failure(
    client: AsyncClient,
    clock: MutableClock,
    test_settings: Settings,
    db_session: AsyncSession,
    user_factory,
    auth_headers_factory,
) -> None:
    owner = await user_factory("owner-out@example.com", role=UserRole.OWNER)
    member = await user_factory("member-out@example.com", role=UserRole.MEMBER)

    res_resp = await client.post(
        "/api/v1/resources",
        json={
            "name": "Boardroom B",
            "resource_type": "MEETING_ROOM",
            "capacity": 8,
            "hourly_rate_cents": 3000,
        },
        headers=auth_headers_factory(owner),
    )
    booking_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": res_resp.json()["id"],
            "start_time": "2026-12-01T15:00:00Z",
            "end_time": "2026-12-01T16:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    booking = booking_resp.json()

    ts = int(clock.now().timestamp())
    raw_body = json.dumps(
        {
            "provider": "payos",
            "event_id": "evt_outbox_01",
            "event_type": "payment.succeeded",
            "booking_id": booking["id"],
            "payment_reference": "PAY-OUT-1",
            "amount_cents": booking["total_price_cents"],
        }
    ).encode("utf-8")
    sig = compute_webhook_signature(test_settings.webhook_secret, ts, raw_body)
    wh_resp = await client.post(
        "/api/v1/webhooks/payment",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": str(ts),
            "X-Webhook-Signature": sig,
        },
    )
    assert wh_resp.status_code == 200

    class FailingSender:
        async def send(self, notification: OutboxNotification) -> None:
            raise RuntimeError("SMTP temporarily unreachable")

    service_fail = MaintenanceService(
        session=db_session,
        clock=clock,
        lock_manager=ResourceLockManager(),
        sender=FailingSender(),
    )
    assert await service_fail.process_pending_outbox() == 0
    assert await service_fail.process_pending_outbox() == 0
    assert await service_fail.process_pending_outbox() == 0

    row = (await db_session.execute(select(OutboxNotification))).scalar_one()
    assert row.attempts == 3
    assert row.status == "FAILED"
