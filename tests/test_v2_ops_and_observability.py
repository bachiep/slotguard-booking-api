"""Tests for v2.0 middleware, metrics summary, outbox inspector, demo signer, and console."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from httpx import AsyncClient

from slotguard.core.clock import MutableClock
from slotguard.core.config import Settings
from slotguard.core.metrics import SlotGuardMetrics
from slotguard.models.user import UserRole
from slotguard.workers.maintenance_worker import MaintenanceWorker


async def test_request_context_middleware_propagates_request_id_timing_and_logs(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="slotguard.http"):
        auto_resp = await client.get("/api/v1/health")
        assert auto_resp.status_code == 200
        assert auto_resp.headers["X-Request-Id"]
        assert float(auto_resp.headers["X-Response-Time-Ms"]) >= 0.0

        traced_resp = await client.get(
            "/api/v1/health",
            headers={"X-Request-Id": "trace-v2-abc-123"},
        )
        assert traced_resp.status_code == 200
        assert traced_resp.headers["X-Request-Id"] == "trace-v2-abc-123"
        assert float(traced_resp.headers["X-Response-Time-Ms"]) >= 0.0

    matching = [
        rec for rec in caplog.records if getattr(rec, "request_id", None) == "trace-v2-abc-123"
    ]
    assert len(matching) >= 1


async def test_problem_detail_includes_request_id_across_error_types(
    client: AsyncClient,
    user_factory,
    auth_headers_factory,
) -> None:
    not_found = await client.get(
        "/api/v1/does-not-exist",
        headers={"X-Request-Id": "req-err-404"},
    )
    assert not_found.status_code == 404
    assert not_found.headers["X-Request-Id"] == "req-err-404"
    assert not_found.headers["content-type"].startswith("application/problem+json")
    nf_body = not_found.json()
    assert nf_body["request_id"] == "req-err-404"
    assert nf_body["status"] == 404
    assert nf_body["instance"] == "/api/v1/does-not-exist"

    member = await user_factory("err-member@example.com", role=UserRole.MEMBER)
    validation_err = await client.post(
        "/api/v1/bookings",
        json={"resource_id": 0, "start_time": "bad", "end_time": "bad"},
        headers={**auth_headers_factory(member), "X-Request-Id": "req-err-422"},
    )
    assert validation_err.status_code == 422
    assert validation_err.json()["request_id"] == "req-err-422"
    assert isinstance(validation_err.json()["errors"], list)


async def test_metrics_summary_tracks_bookings_conflicts_webhooks_rate_limits_and_worker(
    app,
    client: AsyncClient,
    clock: MutableClock,
    test_settings: Settings,
    db_engine_and_session_maker,
    user_factory,
    auth_headers_factory,
) -> None:
    _, session_maker = db_engine_and_session_maker
    owner = await user_factory("metrics-owner@example.com", role=UserRole.OWNER)
    member = await user_factory(
        "metrics-member@example.com",
        password="Password123!",
        role=UserRole.MEMBER,
    )

    res_resp = await client.post(
        "/api/v1/resources",
        json={
            "name": "Telemetry Studio",
            "resource_type": "STUDIO",
            "capacity": 4,
            "hourly_rate_cents": 4000,
            "open_time": "08:00:00",
            "close_time": "20:00:00",
            "slot_duration_minutes": 60,
        },
        headers=auth_headers_factory(owner),
    )
    assert res_resp.status_code == 201
    resource_id = res_resp.json()["id"]

    # Create HOLD #1 and trigger 409 overlap conflict
    hold_1 = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T10:00:00Z",
            "end_time": "2026-12-01T11:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert hold_1.status_code == 201
    booking_1_id = hold_1.json()["id"]

    conflict = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T10:00:00Z",
            "end_time": "2026-12-01T11:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert conflict.status_code == 409

    # Create HOLD #2 to expire later via MaintenanceWorker
    hold_2 = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T12:00:00Z",
            "end_time": "2026-12-01T13:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    assert hold_2.status_code == 201

    # Sign HOLD #1 via demo-sign, send 1 tampered (401) + 1 valid (200) + 2 replays (200)
    sign_resp = await client.post(
        "/api/v1/webhooks/demo-sign",
        json={"booking_id": booking_1_id, "provider": "payos", "event_id": "evt_metrics_01"},
    )
    assert sign_resp.status_code == 200
    signed = sign_resp.json()

    tampered = await client.post(
        "/api/v1/webhooks/payment",
        content=signed["raw_body"].encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": signed["timestamp"],
            "X-Webhook-Signature": "sha256=" + "0" * 64,
        },
    )
    assert tampered.status_code == 401

    valid_headers = {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": signed["timestamp"],
        "X-Webhook-Signature": signed["signature"],
    }
    for expected_status in ("processed", "already_processed", "already_processed"):
        wh = await client.post(
            "/api/v1/webhooks/payment",
            content=signed["raw_body"].encode("utf-8"),
            headers=valid_headers,
        )
        assert wh.status_code == 200
        assert wh.json()["status"] == expected_status

    # Trigger a 429 rate-limit hit on login
    test_settings.rate_limit_auth_requests = 1
    login_body = {"email": "metrics-member@example.com", "password": "Password123!"}
    assert (await client.post("/api/v1/auth/login", json=login_body)).status_code == 200
    assert (await client.post("/api/v1/auth/login", json=login_body)).status_code == 429

    # Advance clock past hold TTL and run maintenance worker cycle
    clock.advance(minutes=16)
    worker = MaintenanceWorker(
        session_maker=session_maker,
        clock=clock,
        lock_manager=app.state.resource_locks,
        metrics=app.state.metrics,
    )
    expired_count, sent_count = await worker.run_once()
    assert expired_count == 1
    assert sent_count == 2

    summary_resp = await client.get("/api/v1/metrics/summary")
    assert summary_resp.status_code == 200
    summary = summary_resp.json()

    assert summary["version"] == "2.1.0"
    assert summary["requests"]["total"] >= 11
    assert summary["requests"]["status_2xx"] >= 8
    assert summary["requests"]["status_4xx"] >= 3
    assert summary["requests"]["status_5xx"] == 0
    assert summary["requests"]["rate_limit_hits_429"] == 1
    assert summary["latency_ms"]["p50"] >= 0.0
    assert summary["latency_ms"]["p95"] >= summary["latency_ms"]["p50"]
    assert summary["bookings"]["holds_created"] == 2
    assert summary["bookings"]["conflicts_blocked_409"] == 1
    assert summary["webhooks"]["verified"] == 3
    assert summary["webhooks"]["processed"] == 1
    assert summary["webhooks"]["idempotent_replays"] == 2
    assert summary["webhooks"]["rejected"] == 1
    assert summary["maintenance"]["expired_holds"] == 1
    assert summary["maintenance"]["outbox_processed"] == 2
    assert summary["database_totals"]["bookings_confirmed"] == 1
    assert summary["database_totals"]["bookings_expired"] == 1
    assert summary["database_totals"]["outbox_sent"] == 2
    assert test_settings.webhook_secret not in json.dumps(summary)
    assert test_settings.jwt_secret not in json.dumps(summary)


def test_slot_guard_metrics_thread_safety(clock: MutableClock) -> None:
    metrics = SlotGuardMetrics(clock=clock)

    def _worker(idx: int) -> None:
        for step in range(25):
            metrics.record_request(201 if step % 2 == 0 else 409, float(idx + step + 1))
            metrics.record_hold_created()
            metrics.record_booking_conflict()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_worker, range(4)))

    snap = metrics.snapshot()
    assert snap.requests_total == 100
    assert snap.status_2xx == 52
    assert snap.status_4xx == 48
    assert snap.holds_created == 100
    assert snap.conflicts_blocked_409 == 100
    assert snap.latency_sample_count == 100
    assert snap.latency_p95_ms >= snap.latency_p50_ms > 0.0


async def test_outbox_recent_returns_newest_rows_with_safe_summary(
    client: AsyncClient,
    user_factory,
    auth_headers_factory,
) -> None:
    owner = await user_factory("outbox-owner@example.com", role=UserRole.OWNER)
    member = await user_factory("outbox-member@example.com", role=UserRole.MEMBER)

    res_resp = await client.post(
        "/api/v1/resources",
        json={
            "name": "Outbox Room",
            "resource_type": "MEETING_ROOM",
            "capacity": 6,
            "hourly_rate_cents": 2000,
        },
        headers=auth_headers_factory(owner),
    )
    resource_id = res_resp.json()["id"]

    booking_ids: list[int] = []
    for hour in (10, 11):
        b = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": resource_id,
                "start_time": f"2026-12-01T{hour:02d}:00:00Z",
                "end_time": f"2026-12-01T{hour + 1:02d}:00:00Z",
            },
            headers=auth_headers_factory(member),
        )
        assert b.status_code == 201
        booking_ids.append(b.json()["id"])

    for idx, booking_id in enumerate(booking_ids, start=1):
        sign_resp = await client.post(
            "/api/v1/webhooks/demo-sign",
            json={"booking_id": booking_id, "event_id": f"evt_outbox_recent_{idx}"},
        )
        signed = sign_resp.json()
        wh = await client.post(
            "/api/v1/webhooks/payment",
            content=signed["raw_body"].encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Timestamp": signed["timestamp"],
                "X-Webhook-Signature": signed["signature"],
            },
        )
        assert wh.status_code == 200

    recent_one = await client.get("/api/v1/outbox/recent", params={"limit": 1})
    assert recent_one.status_code == 200
    body = recent_one.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    latest = body["items"][0]
    assert latest["booking_id"] == booking_ids[1]
    assert latest["event_type"] == "BOOKING_CONFIRMED"
    assert latest["status"] == "PENDING"
    assert latest["attempts"] == 0
    assert latest["payload_summary"]["booking_id"] == booking_ids[1]
    assert latest["payload_summary"]["amount_cents"] == 2000
    assert "recipient_email" not in latest


async def test_webhook_demo_sign_succeeds_in_non_prod_and_blocks_production(
    client: AsyncClient,
    test_settings: Settings,
    user_factory,
    auth_headers_factory,
) -> None:
    owner = await user_factory("sign-owner@example.com", role=UserRole.OWNER)
    member = await user_factory("sign-member@example.com", role=UserRole.MEMBER)

    res_resp = await client.post(
        "/api/v1/resources",
        json={
            "name": "Signer Studio",
            "resource_type": "STUDIO",
            "capacity": 2,
            "hourly_rate_cents": 3500,
        },
        headers=auth_headers_factory(owner),
    )
    booking_resp = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": res_resp.json()["id"],
            "start_time": "2026-12-01T13:00:00Z",
            "end_time": "2026-12-01T14:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    booking_id = booking_resp.json()["id"]

    missing_resp = await client.post("/api/v1/webhooks/demo-sign", json={"booking_id": 99999})
    assert missing_resp.status_code == 404

    sign_resp = await client.post(
        "/api/v1/webhooks/demo-sign",
        json={"booking_id": booking_id},
    )
    assert sign_resp.status_code == 200
    signed = sign_resp.json()
    assert signed["booking_id"] == booking_id
    assert signed["amount_cents"] == 3500
    assert signed["signature"].startswith("sha256=")

    wh_resp = await client.post(
        "/api/v1/webhooks/payment",
        content=signed["raw_body"].encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": signed["timestamp"],
            "X-Webhook-Signature": signed["signature"],
        },
    )
    assert wh_resp.status_code == 200
    assert wh_resp.json()["booking_status"] == "CONFIRMED"

    test_settings.app_env = "production"
    prod_resp = await client.post(
        "/api/v1/webhooks/demo-sign",
        json={"booking_id": booking_id},
        headers={"X-Request-Id": "prod-block-1"},
    )
    assert prod_resp.status_code == 403
    assert prod_resp.headers["content-type"].startswith("application/problem+json")
    assert prod_resp.json()["request_id"] == "prod-block-1"


async def test_availability_returns_slot_statuses_and_booking_ids(
    client: AsyncClient,
    user_factory,
    auth_headers_factory,
) -> None:
    owner = await user_factory("avail-owner@example.com", role=UserRole.OWNER)
    member = await user_factory("avail-member@example.com", role=UserRole.MEMBER)

    res_resp = await client.post(
        "/api/v1/resources",
        json={
            "name": "Timeline Room",
            "resource_type": "MEETING_ROOM",
            "capacity": 8,
            "hourly_rate_cents": 2500,
            "open_time": "08:00:00",
            "close_time": "11:00:00",
            "slot_duration_minutes": 60,
        },
        headers=auth_headers_factory(owner),
    )
    resource_id = res_resp.json()["id"]

    hold = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": resource_id,
            "start_time": "2026-12-01T09:00:00Z",
            "end_time": "2026-12-01T10:00:00Z",
        },
        headers=auth_headers_factory(member),
    )
    booking_id = hold.json()["id"]

    avail = await client.get(
        f"/api/v1/resources/{resource_id}/availability",
        params={"date": "2026-12-01"},
    )
    assert avail.status_code == 200
    slots = avail.json()["slots"]
    assert [(s["status"], s["available"], s["booking_id"]) for s in slots] == [
        ("FREE", True, None),
        ("HOLD", False, booking_id),
        ("FREE", True, None),
    ]


async def test_root_console_serves_html_without_secrets(
    client: AsyncClient,
    test_settings: Settings,
) -> None:
    resp = await client.get("/", headers={"X-Request-Id": "ui-console-1"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["X-Request-Id"] == "ui-console-1"
    assert float(resp.headers["X-Response-Time-Ms"]) >= 0.0
    assert "SlotGuard v2.1.0 — Timeline &amp; Concurrency Lab" in resp.text
    assert test_settings.webhook_secret not in resp.text
    assert test_settings.jwt_secret not in resp.text


async def test_openapi_documents_v2_endpoints_and_problem_detail(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert "/api/v1/metrics/summary" in spec["paths"]
    assert "/api/v1/outbox/recent" in spec["paths"]
    assert "/api/v1/webhooks/demo-sign" in spec["paths"]
    assert "ProblemDetail" in spec["components"]["schemas"]
