"""Comprehensive unit and API tests for SlotGuard v2.1.0 features.

Tests cover:
- Atomic booking reschedule (self-shift non-conflict, 409 conflict, cancelled/expired 409, RBAC 403)
- RFC-5545 .ics iCalendar file export (CRLF termination, sanitized headers, RBAC 403)
- Non-production on-demand maintenance sweep (/api/v1/ops/run-maintenance, production 403 guard)
- OpenAPI schema exposure of v2.1 endpoints
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from slotguard.core.clock import MutableClock
from slotguard.core.config import Settings
from slotguard.main import create_app
from slotguard.models.booking import Booking, BookingStatus
from slotguard.models.user import User, UserRole


async def _create_resource(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str = "Meeting Room Alpha",
    open_time: str = "08:00:00",
    close_time: str = "20:00:00",
    slot_duration_minutes: int = 60,
    hourly_rate_cents: int = 3000,
) -> int:
    resp = await client.post(
        "/api/v1/resources",
        json={
            "name": name,
            "resource_type": "MEETING_ROOM",
            "capacity": 6,
            "hourly_rate_cents": hourly_rate_cents,
            "open_time": open_time,
            "close_time": close_time,
            "slot_duration_minutes": slot_duration_minutes,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def test_reschedule_success_shifts_interval_and_refreshes_hold_expires_at(
    client: AsyncClient,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
    clock: MutableClock,
) -> None:
    owner = await user_factory("owner-reschedule-1@example.com", role=UserRole.OWNER)
    member = await user_factory("member-reschedule-1@example.com", role=UserRole.MEMBER)
    resource_id = await _create_resource(client, auth_headers_factory(owner))

    # Create initial HOLD booking 09:00 - 10:00 UTC
    booking_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T09:00:00Z",
            "end_time": "2026-12-01T10:00:00Z",
            "notes": "Original slot",
        },
        headers=auth_headers_factory(member),
    )
    assert booking_resp.status_code == 201
    booking_id = booking_resp.json()["id"]
    initial_hold_expires_at = booking_resp.json()["hold_expires_at"]

    # Advance clock by 5 minutes
    clock.advance(timedelta(minutes=5))

    # Reschedule to 11:00 - 12:00 UTC
    reschedule_resp = await client.post(
        f"/api/v1/bookings/{booking_id}/reschedule",
        json={
            "start_time": "2026-12-01T11:00:00Z",
            "end_time": "2026-12-01T12:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert reschedule_resp.status_code == 200, reschedule_resp.text
    data = reschedule_resp.json()
    assert data["id"] == booking_id
    assert data["start_time"] == "2026-12-01T11:00:00Z"
    assert data["end_time"] == "2026-12-01T12:00:00Z"
    assert data["status"] == "HOLD"
    # Verify hold_expires_at was refreshed
    assert data["hold_expires_at"] > initial_hold_expires_at


async def test_reschedule_confirmed_booking_succeeds(
    client: AsyncClient,
    db_session: AsyncSession,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    owner = await user_factory("owner-conf-reschedule@example.com", role=UserRole.OWNER)
    member = await user_factory("member-conf-reschedule@example.com", role=UserRole.MEMBER)
    resource_id = await _create_resource(client, auth_headers_factory(owner))

    booking_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T09:00:00Z",
            "end_time": "2026-12-01T10:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert booking_resp.status_code == 201
    booking_id = booking_resp.json()["id"]

    # Manually transition booking to CONFIRMED
    booking_row = (
        await db_session.execute(select(Booking).where(Booking.id == booking_id))
    ).scalar_one()
    booking_row.status = BookingStatus.CONFIRMED.value
    await db_session.commit()

    # Reschedule confirmed booking to 14:00 - 15:00 UTC
    res_resp = await client.post(
        f"/api/v1/bookings/{booking_id}/reschedule",
        json={
            "start_time": "2026-12-01T14:00:00Z",
            "end_time": "2026-12-01T15:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert res_resp.status_code == 200
    data = res_resp.json()
    assert data["status"] == "CONFIRMED"
    assert data["start_time"] == "2026-12-01T14:00:00Z"
    assert data["end_time"] == "2026-12-01T15:00:00Z"


async def test_reschedule_self_overlap_non_conflict(
    client: AsyncClient,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    owner = await user_factory("owner-self-overlap@example.com", role=UserRole.OWNER)
    member = await user_factory("member-self-overlap@example.com", role=UserRole.MEMBER)
    resource_id = await _create_resource(client, auth_headers_factory(owner))

    # Member books 09:00 - 11:00 (2 hours)
    booking_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T09:00:00Z",
            "end_time": "2026-12-01T11:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert booking_resp.status_code == 201
    booking_id = booking_resp.json()["id"]

    # Reschedule to 10:00 - 12:00 (shifts by 1 hour, partially overlaps old interval 10:00-11:00)
    res_resp = await client.post(
        f"/api/v1/bookings/{booking_id}/reschedule",
        json={
            "start_time": "2026-12-01T10:00:00Z",
            "end_time": "2026-12-01T12:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert res_resp.status_code == 200
    data = res_resp.json()
    assert data["start_time"] == "2026-12-01T10:00:00Z"
    assert data["end_time"] == "2026-12-01T12:00:00Z"


async def test_reschedule_conflict_with_another_booking_returns_409(
    client: AsyncClient,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    owner = await user_factory("owner-conflict@example.com", role=UserRole.OWNER)
    member1 = await user_factory("member1-conflict@example.com", role=UserRole.MEMBER)
    member2 = await user_factory("member2-conflict@example.com", role=UserRole.MEMBER)
    resource_id = await _create_resource(client, auth_headers_factory(owner))

    # Member 1 books 09:00 - 10:00
    b1 = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T09:00:00Z",
            "end_time": "2026-12-01T10:00:00Z",
        },
        headers=auth_headers_factory(member1),
    )
    assert b1.status_code == 201
    b1_id = b1.json()["id"]

    # Member 2 books 11:00 - 12:00
    b2 = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T11:00:00Z",
            "end_time": "2026-12-01T12:00:00Z",
        },
        headers=auth_headers_factory(member2),
    )
    assert b2.status_code == 201

    # Member 1 tries to reschedule into Member 2's slot 11:00 - 12:00
    conflict = await client.post(
        f"/api/v1/bookings/{b1_id}/reschedule",
        json={
            "start_time": "2026-12-01T11:00:00Z",
            "end_time": "2026-12-01T12:00:00Z",
        },
        headers=auth_headers_factory(member1),
    )
    assert conflict.status_code == 409
    detail = conflict.json()
    assert detail["title"] == "Conflict"
    assert "overlaps" in detail["detail"]
    assert "request_id" in detail


async def test_reschedule_cancelled_or_expired_returns_409(
    client: AsyncClient,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    owner = await user_factory("owner-cancelled-reschedule@example.com", role=UserRole.OWNER)
    member = await user_factory("member-cancelled-reschedule@example.com", role=UserRole.MEMBER)
    resource_id = await _create_resource(client, auth_headers_factory(owner))

    # Book and cancel
    b = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T09:00:00Z",
            "end_time": "2026-12-01T10:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    booking_id = b.json()["id"]

    cancel = await client.post(
        f"/api/v1/bookings/{booking_id}/cancel",
        headers=auth_headers_factory(member),
    )
    assert cancel.status_code == 200

    # Reschedule cancelled booking -> 409
    res = await client.post(
        f"/api/v1/bookings/{booking_id}/reschedule",
        json={
            "start_time": "2026-12-01T13:00:00Z",
            "end_time": "2026-12-01T14:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert res.status_code == 409
    assert "Cannot reschedule a booking in 'CANCELLED' status" in res.json()["detail"]


async def test_reschedule_rbac_forbidden_for_other_member_and_allowed_for_owner(
    client: AsyncClient,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    owner = await user_factory("owner-rbac@example.com", role=UserRole.OWNER)
    member1 = await user_factory("member1-rbac@example.com", role=UserRole.MEMBER)
    member2 = await user_factory("member2-rbac@example.com", role=UserRole.MEMBER)
    resource_id = await _create_resource(client, auth_headers_factory(owner))

    b = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T09:00:00Z",
            "end_time": "2026-12-01T10:00:00Z",
        },
        headers=auth_headers_factory(member1),
    )
    booking_id = b.json()["id"]

    # Member 2 tries to reschedule Member 1's booking -> 403
    forbidden = await client.post(
        f"/api/v1/bookings/{booking_id}/reschedule",
        json={
            "start_time": "2026-12-01T13:00:00Z",
            "end_time": "2026-12-01T14:00:00Z",
        },
        headers=auth_headers_factory(member2),
    )
    assert forbidden.status_code == 403

    # Owner can reschedule Member 1's booking -> 200
    owner_res = await client.post(
        f"/api/v1/bookings/{booking_id}/reschedule",
        json={
            "start_time": "2026-12-01T13:00:00Z",
            "end_time": "2026-12-01T14:00:00Z",
        },
        headers=auth_headers_factory(owner),
    )
    assert owner_res.status_code == 200


async def test_reschedule_validation_and_not_found(
    client: AsyncClient,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    member = await user_factory("member-val-res@example.com", role=UserRole.MEMBER)
    headers = auth_headers_factory(member)

    # 404 for unknown booking
    nf = await client.post(
        "/api/v1/bookings/999999/reschedule",
        json={
            "start_time": "2026-12-01T10:00:00Z",
            "end_time": "2026-12-01T11:00:00Z",
        },
        headers=headers,
    )
    assert nf.status_code == 404

    # 422 for inverted interval (start >= end)
    inv = await client.post(
        "/api/v1/bookings/999999/reschedule",
        json={
            "start_time": "2026-12-01T11:00:00Z",
            "end_time": "2026-12-01T10:00:00Z",
        },
        headers=headers,
    )
    assert inv.status_code == 422


async def test_ics_export_returns_200_with_valid_crlf_and_rfc5545_headers(
    client: AsyncClient,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    owner = await user_factory("owner-ics@example.com", role=UserRole.OWNER)
    member = await user_factory("member-ics@example.com", role=UserRole.MEMBER)
    resource_id = await _create_resource(
        client, auth_headers_factory(owner), name="Boardroom Executive"
    )

    b = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T10:00:00Z",
            "end_time": "2026-12-01T11:00:00Z",
            "notes": "Quarterly Strategy Sync",
        },
        headers=auth_headers_factory(member),
    )
    booking_id = b.json()["id"]

    resp = await client.get(
        f"/api/v1/bookings/{booking_id}/ics",
        headers=auth_headers_factory(member),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/calendar")
    assert (
        resp.headers["content-disposition"]
        == f'attachment; filename="slotguard-booking-{booking_id}.ics"'
    )

    body = resp.text
    # Verify strict CRLF termination
    assert "\r\n" in body
    lines = body.split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR"
    assert "VERSION:2.0" in lines
    assert "PRODID:-//SlotGuard//Booking API v2.1//EN" in lines
    assert "BEGIN:VEVENT" in lines
    assert f"UID:slotguard-booking-{booking_id}@slotguard.dev" in lines
    assert "DTSTART:20261201T100000Z" in lines
    assert "DTEND:20261201T110000Z" in lines
    assert f"SUMMARY:SlotGuard Booking #{booking_id} - Boardroom Executive" in lines
    assert "Quarterly Strategy Sync" in body
    assert "STATUS:HOLD" in lines
    assert "END:VEVENT" in lines
    assert "END:VCALENDAR" in lines


async def test_ics_export_rbac_forbidden_for_other_member_and_allowed_for_owner(
    client: AsyncClient,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    owner = await user_factory("owner-ics-rbac@example.com", role=UserRole.OWNER)
    member1 = await user_factory("member1-ics-rbac@example.com", role=UserRole.MEMBER)
    member2 = await user_factory("member2-ics-rbac@example.com", role=UserRole.MEMBER)
    resource_id = await _create_resource(client, auth_headers_factory(owner))

    b = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T10:00:00Z",
            "end_time": "2026-12-01T11:00:00Z",
        },
        headers=auth_headers_factory(member1),
    )
    booking_id = b.json()["id"]

    # Member 2 is forbidden
    f_resp = await client.get(
        f"/api/v1/bookings/{booking_id}/ics",
        headers=auth_headers_factory(member2),
    )
    assert f_resp.status_code == 403

    # Owner is allowed
    o_resp = await client.get(
        f"/api/v1/bookings/{booking_id}/ics",
        headers=auth_headers_factory(owner),
    )
    assert o_resp.status_code == 200

    # Missing booking is 404
    nf_resp = await client.get(
        "/api/v1/bookings/888888/ics",
        headers=auth_headers_factory(owner),
    )
    assert nf_resp.status_code == 404


async def test_run_maintenance_endpoint_releases_expired_holds_and_sends_outbox(
    client: AsyncClient,
    db_session: AsyncSession,
    clock: MutableClock,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    owner = await user_factory("owner-maint@example.com", role=UserRole.OWNER)
    member = await user_factory("member-maint@example.com", role=UserRole.MEMBER)
    resource_id = await _create_resource(client, auth_headers_factory(owner))

    # Create HOLD booking
    b = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T10:00:00Z",
            "end_time": "2026-12-01T11:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    booking_id = b.json()["id"]

    # Advance clock past the 15-minute hold TTL (20 minutes)
    clock.advance(timedelta(minutes=20))

    # Trigger maintenance sweep endpoint
    resp = await client.post("/api/v1/ops/run-maintenance")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["expired_holds"] >= 1
    assert data["outbox_sent"] >= 1
    assert data["outbox_failed"] == 0

    # Verify booking status in database is now EXPIRED
    booking_row = (
        await db_session.execute(select(Booking).where(Booking.id == booking_id))
    ).scalar_one()
    assert booking_row.status == BookingStatus.EXPIRED.value

    # Verify slot is now free to be booked again
    rebook = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T10:00:00Z",
            "end_time": "2026-12-01T11:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert rebook.status_code == 201


async def test_run_maintenance_blocked_in_production_returns_403(
    clock: MutableClock,
    db_engine_and_session_maker: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, session_maker = db_engine_and_session_maker
    prod_settings = Settings(
        app_env="production",
        environment="production",
        database_url="sqlite+aiosqlite:///:memory:",
        rate_limit_backend="memory",
        worker_enabled=False,
        jwt_secret="prod-secret-must-be-at-least-32-chars-long",
        webhook_secret="prod-webhook-secret-at-least-32-chars",
    )
    prod_app = create_app(
        settings=prod_settings,
        clock=clock,
        engine=engine,
        session_maker=session_maker,
    )
    transport = ASGITransport(app=prod_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as prod_client:
        resp = await prod_client.post("/api/v1/ops/run-maintenance")
        assert resp.status_code == 403
        detail = resp.json()
        assert detail["title"] == "Forbidden"
        assert "disabled in production" in detail["detail"].lower()


async def test_openapi_documents_v21_endpoints(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    paths = spec["paths"]
    assert "/api/v1/bookings/{booking_id}/reschedule" in paths
    assert "/api/v1/bookings/{booking_id}/ics" in paths
    assert "/api/v1/ops/run-maintenance" in paths
    schemas = spec["components"]["schemas"]
    assert "BookingRescheduleRequest" in schemas
    assert "MaintenanceRunResponse" in schemas


async def test_bookings_list_and_get_details(
    client: AsyncClient,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    owner = await user_factory("owner-list-get@example.com", role=UserRole.OWNER)
    member1 = await user_factory("member1-list-get@example.com", role=UserRole.MEMBER)
    member2 = await user_factory("member2-list-get@example.com", role=UserRole.MEMBER)
    resource_id = await _create_resource(client, auth_headers_factory(owner))

    # Member 1 creates booking
    b_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T09:00:00Z",
            "end_time": "2026-12-01T10:00:00Z",
        },
        headers=auth_headers_factory(member1),
    )
    assert b_resp.status_code == 201
    b_id = b_resp.json()["id"]

    # Member 1 lists bookings -> sees 1
    m1_list = await client.get("/api/v1/bookings", headers=auth_headers_factory(member1))
    assert m1_list.status_code == 200
    assert m1_list.json()["total"] == 1
    assert m1_list.json()["items"][0]["id"] == b_id

    # Member 2 lists bookings -> sees 0
    m2_list = await client.get("/api/v1/bookings", headers=auth_headers_factory(member2))
    assert m2_list.status_code == 200
    assert m2_list.json()["total"] == 0

    # Owner lists all bookings -> sees 1
    owner_list = await client.get("/api/v1/bookings", headers=auth_headers_factory(owner))
    assert owner_list.status_code == 200
    assert owner_list.json()["total"] == 1

    # Owner lists with mine=true -> sees 0
    owner_mine = await client.get(
        "/api/v1/bookings",
        params={"mine": "true"},
        headers=auth_headers_factory(owner),
    )
    assert owner_mine.status_code == 200
    assert owner_mine.json()["total"] == 0

    # Member 1 gets their booking -> 200
    m1_get = await client.get(f"/api/v1/bookings/{b_id}", headers=auth_headers_factory(member1))
    assert m1_get.status_code == 200

    # Member 2 gets Member 1's booking -> 403
    m2_get = await client.get(f"/api/v1/bookings/{b_id}", headers=auth_headers_factory(member2))
    assert m2_get.status_code == 403

    # Owner gets Member 1's booking -> 200
    owner_get = await client.get(f"/api/v1/bookings/{b_id}", headers=auth_headers_factory(owner))
    assert owner_get.status_code == 200

    # Unknown booking -> 404
    nf = await client.get("/api/v1/bookings/999999", headers=auth_headers_factory(owner))
    assert nf.status_code == 404


async def test_booking_edge_cases_inactive_resource_and_expired_reschedule(
    client: AsyncClient,
    db_session: AsyncSession,
    clock: MutableClock,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    owner = await user_factory("owner-inactive@example.com", role=UserRole.OWNER)
    member = await user_factory("member-inactive@example.com", role=UserRole.MEMBER)
    resource_id = await _create_resource(client, auth_headers_factory(owner))

    # Create active hold
    b_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T09:00:00Z",
            "end_time": "2026-12-01T10:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    b_id = b_resp.json()["id"]

    # Advance clock past hold TTL without running maintenance
    clock.advance(timedelta(minutes=20))

    # Try to reschedule expired hold -> 409
    res = await client.post(
        f"/api/v1/bookings/{b_id}/reschedule",
        json={
            "start_time": "2026-12-01T11:00:00Z",
            "end_time": "2026-12-01T12:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert res.status_code == 409
    assert "expired HOLD" in res.json()["detail"]


async def test_resource_service_update_and_duplicate_error(
    client: AsyncClient,
    user_factory: Callable[..., Any],
    auth_headers_factory: Callable[[User], dict[str, str]],
) -> None:
    owner1 = await user_factory("owner1-crud@example.com", role=UserRole.OWNER)
    owner2 = await user_factory("owner2-crud@example.com", role=UserRole.OWNER)
    resource_id = await _create_resource(client, auth_headers_factory(owner1), name="Original Name")

    # Creating duplicate name -> 409
    dup = await client.post(
        "/api/v1/resources",
        json={
            "name": "Original Name",
            "resource_type": "MEETING_ROOM",
            "capacity": 4,
            "hourly_rate_cents": 2000,
            "open_time": "08:00:00",
            "close_time": "18:00:00",
            "slot_duration_minutes": 30,
        },
        headers=auth_headers_factory(owner1),
    )
    assert dup.status_code == 409

    # Owner 2 cannot update Owner 1's resource -> 403
    forb = await client.patch(
        f"/api/v1/resources/{resource_id}",
        json={"name": "Hacked Name"},
        headers=auth_headers_factory(owner2),
    )
    assert forb.status_code == 403

    # Update with open >= close -> 422
    inv = await client.patch(
        f"/api/v1/resources/{resource_id}",
        json={"open_time": "18:00:00", "close_time": "08:00:00"},
        headers=auth_headers_factory(owner1),
    )
    assert inv.status_code == 422

    # Valid update
    ok = await client.patch(
        f"/api/v1/resources/{resource_id}",
        json={"name": "Updated Clean Name", "capacity": 10},
        headers=auth_headers_factory(owner1),
    )
    assert ok.status_code == 200
    assert ok.json()["name"] == "Updated Clean Name"
    assert ok.json()["capacity"] == 10

    # Get resource details
    get_res = await client.get(f"/api/v1/resources/{resource_id}")
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "Updated Clean Name"

    # Get non-existent resource -> 404
    nf_res = await client.get("/api/v1/resources/999999")
    assert nf_res.status_code == 404


async def test_auth_edge_cases(
    client: AsyncClient,
    user_factory: Callable[..., Any],
) -> None:
    # Register user
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "unique-reg@example.com",
            "password": "StrongPassword123!",
            "full_name": "Unique Person",
        },
    )
    assert reg.status_code == 201

    # Duplicate register -> 409
    dup = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "unique-reg@example.com",
            "password": "StrongPassword123!",
            "full_name": "Unique Person",
        },
    )
    assert dup.status_code == 409

    # Bad password login -> 401
    bad_pw = await client.post(
        "/api/v1/auth/login",
        json={"email": "unique-reg@example.com", "password": "WrongPassword!"},
    )
    assert bad_pw.status_code == 401

    # Inactive user login -> 401
    await user_factory("inactive@example.com", is_active=False)
    inact = await client.post(
        "/api/v1/auth/login",
        json={"email": "inactive@example.com", "password": "StrongPass123!"},
    )
    assert inact.status_code == 401


async def test_maintenance_worker_start_and_stop_lifecycle(
    db_engine_and_session_maker: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    clock: MutableClock,
) -> None:
    import asyncio

    from slotguard.core.locks import ResourceLockManager
    from slotguard.workers.maintenance_worker import MaintenanceWorker

    _, session_maker = db_engine_and_session_maker
    lock_manager = ResourceLockManager()
    worker = MaintenanceWorker(
        session_maker=session_maker,
        clock=clock,
        lock_manager=lock_manager,
        poll_interval_seconds=1,
    )
    worker.start()
    assert worker._task is not None
    await asyncio.sleep(0.05)
    await worker.stop()
    assert worker._task is None


async def test_app_lifespan_with_worker_enabled(
    db_engine_and_session_maker: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    clock: MutableClock,
) -> None:
    engine, session_maker = db_engine_and_session_maker
    settings = Settings(
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        rate_limit_backend="memory",
        worker_enabled=True,
        worker_poll_interval_seconds=1,
        jwt_secret="lifespan-secret-must-be-32-chars-long!",
        webhook_secret="lifespan-webhook-secret-32-chars!!",
    )
    test_app = create_app(
        settings=settings,
        clock=clock,
        engine=engine,
        session_maker=session_maker,
    )
    async with test_app.router.lifespan_context(test_app):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as test_client:
            resp = await test_client.get("/api/v1/health")
            assert resp.status_code == 200
