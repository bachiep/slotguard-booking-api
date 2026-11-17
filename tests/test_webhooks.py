"""HMAC-SHA256 webhook signature verification, replay guard, and idempotency tests."""

import asyncio
import json

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import MutableClock
from slotguard.core.config import Settings
from slotguard.core.webhook_security import compute_webhook_signature
from slotguard.models.booking import Booking
from slotguard.models.user import UserRole
from slotguard.models.webhook import OutboxNotification, WebhookEvent


async def _setup_hold_booking(
    client: AsyncClient,
    user_factory,
    auth_headers_factory,
) -> dict:
    owner = await user_factory("owner-wh@example.com", role=UserRole.OWNER)
    member = await user_factory("member-wh@example.com", role=UserRole.MEMBER)

    res_resp = await client.post(
        "/api/v1/resources",
        json={
            "name": "Podcast Studio",
            "resource_type": "STUDIO",
            "capacity": 2,
            "hourly_rate_cents": 5000,
            "open_time": "08:00:00",
            "close_time": "20:00:00",
            "slot_duration_minutes": 60,
        },
        headers=auth_headers_factory(owner),
    )
    assert res_resp.status_code == 201

    booking_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": res_resp.json()["id"],
            "start_time": "2026-12-01T10:00:00Z",
            "end_time": "2026-12-01T11:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert booking_resp.status_code == 201
    return booking_resp.json()


def _sign_body(settings: Settings, ts: int, raw_body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": str(ts),
        "X-Webhook-Signature": compute_webhook_signature(settings.webhook_secret, ts, raw_body),
    }


async def test_webhook_rejects_bad_signature_tampered_bytes_and_stale_timestamp(
    client: AsyncClient,
    clock: MutableClock,
    test_settings: Settings,
    user_factory,
    auth_headers_factory,
) -> None:
    booking = await _setup_hold_booking(client, user_factory, auth_headers_factory)
    ts = int(clock.now().timestamp())
    raw_body = json.dumps(
        {
            "provider": "payos",
            "event_id": "evt_tamper_01",
            "event_type": "payment.succeeded",
            "booking_id": booking["id"],
            "payment_reference": "PAY-100",
            "amount_cents": booking["total_price_cents"],
        }
    ).encode("utf-8")

    # Wrong signature
    bad_sig = await client.post(
        "/api/v1/webhooks/payment",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": str(ts),
            "X-Webhook-Signature": "sha256=" + "0" * 64,
        },
    )
    assert bad_sig.status_code == 401

    # Valid signature for raw_body, but body altered with extra space
    signed_headers = _sign_body(test_settings, ts, raw_body)
    tampered = await client.post(
        "/api/v1/webhooks/payment",
        content=raw_body + b" ",
        headers=signed_headers,
    )
    assert tampered.status_code == 401

    # Expired timestamp (> 300s old)
    stale_ts = ts - 301
    stale_resp = await client.post(
        "/api/v1/webhooks/payment",
        content=raw_body,
        headers=_sign_body(test_settings, stale_ts, raw_body),
    )
    assert stale_resp.status_code == 401
    assert "replay window" in stale_resp.json()["detail"].lower()


async def test_webhook_replays_3x_idempotently_confirm_once_and_write_one_outbox(
    client: AsyncClient,
    clock: MutableClock,
    test_settings: Settings,
    db_session: AsyncSession,
    user_factory,
    auth_headers_factory,
) -> None:
    booking = await _setup_hold_booking(client, user_factory, auth_headers_factory)
    ts = int(clock.now().timestamp())
    # Deliberately non-sorted JSON formatting to ensure verification signs raw bytes
    raw_body = (
        '{"amount_cents": '
        + str(booking["total_price_cents"])
        + ', "event_id": "evt_replay_01", "provider": "vnpay", '
        + '"event_type": "payment.succeeded", '
        + f'"booking_id": {booking["id"]}, "payment_reference": "VNP-999"'
        + "}"
    ).encode("utf-8")
    headers = _sign_body(test_settings, ts, raw_body)

    r1 = await client.post("/api/v1/webhooks/payment", content=raw_body, headers=headers)
    r2 = await client.post("/api/v1/webhooks/payment", content=raw_body, headers=headers)
    r3 = await client.post("/api/v1/webhooks/payment", content=raw_body, headers=headers)

    assert r1.status_code == 200
    assert r1.json()["status"] == "processed"
    assert r1.json()["booking_status"] == "CONFIRMED"

    assert r2.status_code == 200
    assert r2.json()["status"] == "already_processed"
    assert r3.status_code == 200
    assert r3.json()["status"] == "already_processed"

    persisted_booking = (
        await db_session.execute(select(Booking).where(Booking.id == booking["id"]))
    ).scalar_one()
    assert persisted_booking.status == "CONFIRMED"
    assert persisted_booking.payment_reference == "VNP-999"

    events = (await db_session.execute(select(WebhookEvent))).scalars().all()
    outbox = (await db_session.execute(select(OutboxNotification))).scalars().all()
    assert len(events) == 1
    assert len(outbox) == 1
    assert outbox[0].notification_type == "BOOKING_CONFIRMED"
    assert outbox[0].status == "PENDING"


async def test_webhook_concurrent_duplicates_and_payload_conflict(
    client: AsyncClient,
    clock: MutableClock,
    test_settings: Settings,
    db_session: AsyncSession,
    user_factory,
    auth_headers_factory,
) -> None:
    booking = await _setup_hold_booking(client, user_factory, auth_headers_factory)
    ts = int(clock.now().timestamp())
    raw_body = json.dumps(
        {
            "provider": "stripe",
            "event_id": "evt_conc_01",
            "event_type": "payment.succeeded",
            "booking_id": booking["id"],
            "payment_reference": "CH-123",
            "amount_cents": booking["total_price_cents"],
        }
    ).encode("utf-8")
    headers = _sign_body(test_settings, ts, raw_body)

    responses = await asyncio.gather(
        *(
            client.post("/api/v1/webhooks/payment", content=raw_body, headers=headers)
            for _ in range(5)
        )
    )
    assert all(resp.status_code == 200 for resp in responses)
    statuses = [resp.json()["status"] for resp in responses]
    assert statuses.count("processed") == 1
    assert statuses.count("already_processed") == 4

    outbox = (await db_session.execute(select(OutboxNotification))).scalars().all()
    assert len(outbox) == 1

    # Same event_id with tampered payment_reference returns 409 Conflict
    altered_body = json.dumps(
        {
            "provider": "stripe",
            "event_id": "evt_conc_01",
            "event_type": "payment.succeeded",
            "booking_id": booking["id"],
            "payment_reference": "CH-DIFFERENT",
            "amount_cents": booking["total_price_cents"],
        }
    ).encode("utf-8")
    conflict_resp = await client.post(
        "/api/v1/webhooks/payment",
        content=altered_body,
        headers=_sign_body(test_settings, ts, altered_body),
    )
    assert conflict_resp.status_code == 409
