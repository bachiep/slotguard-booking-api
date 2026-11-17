"""Booking creation, half-open overlap guard, concurrency serialization, and cancellation tests."""

import asyncio

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import MutableClock
from slotguard.models.booking import Booking
from slotguard.models.user import UserRole


async def _create_test_resource(
    client: AsyncClient,
    owner_headers: dict[str, str],
    *,
    name: str = "Studio A",
    hourly_rate_cents: int = 4000,
) -> int:
    resp = await client.post(
        "/api/v1/resources",
        json={
            "name": name,
            "resource_type": "STUDIO",
            "capacity": 4,
            "hourly_rate_cents": hourly_rate_cents,
            "open_time": "08:00:00",
            "close_time": "20:00:00",
            "slot_duration_minutes": 60,
        },
        headers=owner_headers,
    )
    assert resp.status_code == 201
    return int(resp.json()["id"])


async def test_adjacent_slots_succeed_and_create_15_min_hold(
    client: AsyncClient,
    user_factory,
    auth_headers_factory,
) -> None:
    owner = await user_factory("owner-adj@example.com", role=UserRole.OWNER)
    member = await user_factory("member-adj@example.com", role=UserRole.MEMBER)
    resource_id = await _create_test_resource(client, auth_headers_factory(owner))

    member_headers = auth_headers_factory(member)
    first = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T09:00:00Z",
            "end_time": "2026-12-01T10:00:00Z",
        },
        headers=member_headers,
    )
    second = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T10:00:00Z",
            "end_time": "2026-12-01T12:00:00Z",
        },
        headers=member_headers,
    )

    assert first.status_code == 201
    assert first.json()["status"] == "HOLD"
    assert first.json()["total_price_cents"] == 4000
    assert first.json()["hold_expires_at"].startswith("2026-12-01T08:15:00")

    assert second.status_code == 201
    assert second.json()["total_price_cents"] == 8000


async def test_overlapping_intervals_return_409_conflict(
    client: AsyncClient,
    user_factory,
    auth_headers_factory,
) -> None:
    owner = await user_factory("owner-ov@example.com", role=UserRole.OWNER)
    member = await user_factory("member-ov@example.com", role=UserRole.MEMBER)
    resource_id = await _create_test_resource(client, auth_headers_factory(owner))
    headers = auth_headers_factory(member)

    base = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T10:00:00Z",
            "end_time": "2026-12-01T12:00:00Z",
        },
        headers=headers,
    )
    assert base.status_code == 201

    overlapping_windows = [
        ("2026-12-01T09:00:00Z", "2026-12-01T11:00:00Z"),
        ("2026-12-01T11:00:00Z", "2026-12-01T13:00:00Z"),
        ("2026-12-01T10:00:00Z", "2026-12-01T11:00:00Z"),
        ("2026-12-01T09:00:00Z", "2026-12-01T13:00:00Z"),
    ]
    for start_iso, end_iso in overlapping_windows:
        conflict = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": resource_id,
                "start_time": start_iso,
                "end_time": end_iso,
            },
            headers=headers,
        )
        assert conflict.status_code == 409
        assert conflict.headers["content-type"].startswith("application/problem+json")


async def test_concurrent_overlapping_bookings_allow_strictly_one_winner(
    client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    auth_headers_factory,
) -> None:
    owner = await user_factory("owner-conc@example.com", role=UserRole.OWNER)
    resource_id = await _create_test_resource(
        client,
        auth_headers_factory(owner),
        name="Contended Court",
    )

    users = [await user_factory(f"racer{i}@example.com", role=UserRole.MEMBER) for i in range(12)]

    async def attempt_booking(idx: int):
        headers = auth_headers_factory(users[idx])
        start_hour = 10 if idx % 2 == 0 else 11
        return await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": resource_id,
                "start_time": f"2026-12-01T{start_hour:02d}:00:00Z",
                "end_time": f"2026-12-01T{start_hour + 2:02d}:00:00Z",
            },
            headers=headers,
        )

    responses = await asyncio.gather(*(attempt_booking(i) for i in range(len(users))))
    statuses = [resp.status_code for resp in responses]

    assert statuses.count(201) == 1
    assert statuses.count(409) == len(users) - 1

    persisted = (await db_session.execute(select(Booking))).scalars().all()
    assert len(persisted) == 1


async def test_booking_validation_rejects_unaligned_past_and_out_of_hours(
    client: AsyncClient,
    user_factory,
    auth_headers_factory,
) -> None:
    owner = await user_factory("owner-val@example.com", role=UserRole.OWNER)
    member = await user_factory("member-val@example.com", role=UserRole.MEMBER)
    resource_id = await _create_test_resource(client, auth_headers_factory(owner))
    headers = auth_headers_factory(member)

    invalid_payloads = [
        # Unaligned start minute
        ("2026-12-01T09:15:00Z", "2026-12-01T10:15:00Z"),
        # Past start time relative to clock (08:00Z)
        ("2026-12-01T07:00:00Z", "2026-12-01T08:00:00Z"),
        # Outside close_time (20:00Z)
        ("2026-12-01T19:00:00Z", "2026-12-01T21:00:00Z"),
        # Naive timestamp without timezone
        ("2026-12-01T09:00:00", "2026-12-01T10:00:00"),
    ]
    for start_iso, end_iso in invalid_payloads:
        resp = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": resource_id,
                "start_time": start_iso,
                "end_time": end_iso,
            },
            headers=headers,
        )
        assert resp.status_code == 422


async def test_cancel_booking_frees_slot_and_enforces_permissions(
    client: AsyncClient,
    clock: MutableClock,
    user_factory,
    auth_headers_factory,
) -> None:
    owner = await user_factory("owner-can@example.com", role=UserRole.OWNER)
    member_1 = await user_factory("member1-can@example.com", role=UserRole.MEMBER)
    member_2 = await user_factory("member2-can@example.com", role=UserRole.MEMBER)
    resource_id = await _create_test_resource(client, auth_headers_factory(owner))

    create_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T14:00:00Z",
            "end_time": "2026-12-01T15:00:00Z",
        },
        headers=auth_headers_factory(member_1),
    )
    booking_id = create_resp.json()["id"]

    other_get = await client.get(
        f"/api/v1/bookings/{booking_id}",
        headers=auth_headers_factory(member_2),
    )
    assert other_get.status_code == 403

    other_cancel = await client.post(
        f"/api/v1/bookings/{booking_id}/cancel",
        headers=auth_headers_factory(member_2),
    )
    assert other_cancel.status_code == 403

    cancel_resp = await client.post(
        f"/api/v1/bookings/{booking_id}/cancel",
        headers=auth_headers_factory(member_1),
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "CANCELLED"

    # Re-booking the cancelled slot now succeeds
    rebook_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T14:00:00Z",
            "end_time": "2026-12-01T15:00:00Z",
        },
        headers=auth_headers_factory(member_2),
    )
    assert rebook_resp.status_code == 201
    rebook_id = rebook_resp.json()["id"]

    # Advancing clock past start_time prevents cancellation
    clock.advance(minutes=360)
    late_cancel = await client.post(
        f"/api/v1/bookings/{rebook_id}/cancel",
        headers=auth_headers_factory(member_2),
    )
    assert late_cancel.status_code == 409
