"""Resource CRUD, RBAC enforcement, pagination, and slot availability tests."""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import MutableClock
from slotguard.models.booking import Booking, BookingStatus
from slotguard.models.user import UserRole


async def test_owner_can_create_and_update_resource_while_member_is_forbidden(
    client: AsyncClient,
    user_factory,
    auth_headers_factory,
) -> None:
    owner_1 = await user_factory("owner1@example.com", role=UserRole.OWNER)
    owner_2 = await user_factory("owner2@example.com", role=UserRole.OWNER)
    member = await user_factory("member1@example.com", role=UserRole.MEMBER)

    payload = {
        "name": "Boardroom Alpha",
        "resource_type": "MEETING_ROOM",
        "description": "12-seat executive room",
        "capacity": 12,
        "hourly_rate_cents": 5000,
        "open_time": "08:00:00",
        "close_time": "18:00:00",
        "slot_duration_minutes": 60,
    }

    member_resp = await client.post(
        "/api/v1/resources",
        json=payload,
        headers=auth_headers_factory(member),
    )
    assert member_resp.status_code == 403

    create_resp = await client.post(
        "/api/v1/resources",
        json=payload,
        headers=auth_headers_factory(owner_1),
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created["name"] == "Boardroom Alpha"
    assert created["owner_id"] == owner_1.id

    dup_resp = await client.post(
        "/api/v1/resources",
        json=payload,
        headers=auth_headers_factory(owner_1),
    )
    assert dup_resp.status_code == 409

    other_owner_patch = await client.patch(
        f"/api/v1/resources/{created['id']}",
        json={"capacity": 16},
        headers=auth_headers_factory(owner_2),
    )
    assert other_owner_patch.status_code == 403

    patch_resp = await client.patch(
        f"/api/v1/resources/{created['id']}",
        json={"capacity": 16, "hourly_rate_cents": 6500},
        headers=auth_headers_factory(owner_1),
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["capacity"] == 16
    assert patch_resp.json()["hourly_rate_cents"] == 6500


async def test_list_resources_supports_filtering_and_pagination(
    client: AsyncClient,
    user_factory,
    auth_headers_factory,
) -> None:
    owner = await user_factory("owner-list@example.com", role=UserRole.OWNER)
    headers = auth_headers_factory(owner)

    resources_to_create = [
        {
            "name": "Room 1",
            "resource_type": "MEETING_ROOM",
            "capacity": 6,
            "hourly_rate_cents": 2000,
        },
        {
            "name": "Court 1",
            "resource_type": "SPORTS_COURT",
            "capacity": 4,
            "hourly_rate_cents": 3000,
        },
        {"name": "Studio 1", "resource_type": "STUDIO", "capacity": 3, "hourly_rate_cents": 4500},
        {
            "name": "Room 2",
            "resource_type": "MEETING_ROOM",
            "capacity": 10,
            "hourly_rate_cents": 3500,
        },
    ]
    created_ids: list[int] = []
    for item in resources_to_create:
        resp = await client.post("/api/v1/resources", json=item, headers=headers)
        assert resp.status_code == 201
        created_ids.append(resp.json()["id"])

    await client.patch(
        f"/api/v1/resources/{created_ids[-1]}",
        json={"is_active": False},
        headers=headers,
    )

    active_rooms = await client.get(
        "/api/v1/resources",
        params={"resource_type": "MEETING_ROOM", "active_only": True},
    )
    assert active_rooms.status_code == 200
    assert active_rooms.json()["total"] == 1
    assert active_rooms.json()["items"][0]["name"] == "Room 1"

    page = await client.get(
        "/api/v1/resources", params={"active_only": False, "limit": 2, "offset": 1}
    )
    assert page.status_code == 200
    assert page.json()["total"] == 4
    assert len(page.json()["items"]) == 2
    assert [r["name"] for r in page.json()["items"]] == ["Court 1", "Studio 1"]


async def test_daily_availability_respects_active_bookings_and_past_slots(
    client: AsyncClient,
    clock: MutableClock,
    db_session: AsyncSession,
    user_factory,
    auth_headers_factory,
) -> None:
    owner = await user_factory("owner-avail@example.com", role=UserRole.OWNER)
    member = await user_factory("member-avail@example.com", role=UserRole.MEMBER)

    create_resp = await client.post(
        "/api/v1/resources",
        json={
            "name": "Badminton Court A",
            "resource_type": "SPORTS_COURT",
            "capacity": 4,
            "hourly_rate_cents": 2500,
            "open_time": "08:00:00",
            "close_time": "13:00:00",
            "slot_duration_minutes": 60,
        },
        headers=auth_headers_factory(owner),
    )
    resource_id = create_resp.json()["id"]

    # Advance clock from 08:00 to 08:30 so [08:00, 09:00) is in the past
    clock.advance(minutes=30)
    now = clock.now()

    # Interleaved bookings: [09:00, 10:00) HOLD, [10:00, 11:00) CANCELLED,
    # [11:00, 12:00) CONFIRMED, [12:00, 13:00) EXPIRED
    db_session.add_all(
        [
            Booking(
                resource_id=resource_id,
                user_id=member.id,
                start_time=datetime(2026, 12, 1, 9, 0, tzinfo=UTC),
                end_time=datetime(2026, 12, 1, 10, 0, tzinfo=UTC),
                status=BookingStatus.HOLD.value,
                total_price_cents=2500,
                hold_expires_at=now + timedelta(minutes=15),
                created_at=now,
                updated_at=now,
            ),
            Booking(
                resource_id=resource_id,
                user_id=member.id,
                start_time=datetime(2026, 12, 1, 10, 0, tzinfo=UTC),
                end_time=datetime(2026, 12, 1, 11, 0, tzinfo=UTC),
                status=BookingStatus.CANCELLED.value,
                total_price_cents=2500,
                hold_expires_at=now + timedelta(minutes=15),
                cancelled_at=now,
                created_at=now,
                updated_at=now,
            ),
            Booking(
                resource_id=resource_id,
                user_id=member.id,
                start_time=datetime(2026, 12, 1, 11, 0, tzinfo=UTC),
                end_time=datetime(2026, 12, 1, 12, 0, tzinfo=UTC),
                status=BookingStatus.CONFIRMED.value,
                total_price_cents=2500,
                hold_expires_at=now + timedelta(minutes=15),
                confirmed_at=now,
                created_at=now,
                updated_at=now,
            ),
            Booking(
                resource_id=resource_id,
                user_id=member.id,
                start_time=datetime(2026, 12, 1, 12, 0, tzinfo=UTC),
                end_time=datetime(2026, 12, 1, 13, 0, tzinfo=UTC),
                status=BookingStatus.EXPIRED.value,
                total_price_cents=2500,
                hold_expires_at=now - timedelta(minutes=1),
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/resources/{resource_id}/availability",
        params={"date": "2026-12-01"},
    )
    assert resp.status_code == 200
    slots = resp.json()["slots"]
    assert len(slots) == 5
    assert [slot["available"] for slot in slots] == [False, False, True, False, True]
