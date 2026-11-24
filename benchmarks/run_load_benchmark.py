"""Reproducible async load and concurrency benchmark against live Uvicorn + SQLite WAL."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True, slots=True)
class ScenarioStats:
    """Measured throughput, status breakdown, and latency percentiles."""

    name: str
    total_requests: int
    status_counts: dict[str, int]
    elapsed_seconds: float
    rps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    assertions_passed: bool
    notes: str


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, math.ceil(pct * len(sorted_values)) - 1))
    return round(sorted_values[idx], 2)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _summarize(
    name: str,
    statuses: list[int],
    latencies_ms: list[float],
    elapsed_seconds: float,
    *,
    assertions_passed: bool,
    notes: str,
) -> ScenarioStats:
    sorted_lat = sorted(latencies_ms)
    counts: dict[str, int] = {}
    for code in statuses:
        key = str(code)
        counts[key] = counts.get(key, 0) + 1
    total = len(statuses)
    return ScenarioStats(
        name=name,
        total_requests=total,
        status_counts=dict(sorted(counts.items())),
        elapsed_seconds=round(elapsed_seconds, 3),
        rps=round(total / elapsed_seconds, 1) if elapsed_seconds > 0 else 0.0,
        p50_ms=_percentile(sorted_lat, 0.50),
        p95_ms=_percentile(sorted_lat, 0.95),
        p99_ms=_percentile(sorted_lat, 0.99),
        min_ms=round(sorted_lat[0], 2) if sorted_lat else 0.0,
        max_ms=round(sorted_lat[-1], 2) if sorted_lat else 0.0,
        assertions_passed=assertions_passed,
        notes=notes,
    )


async def _wait_for_health(client: httpx.AsyncClient, timeout_seconds: float = 15.0) -> None:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        try:
            resp = await client.get("/api/v1/health")
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.15)
    raise RuntimeError("Timed out waiting for Uvicorn benchmark server health check.")


async def _prepare_database(database_url: str) -> None:
    from scripts.seed_demo import seed_demo_data

    from slotguard.core.database import create_engine_and_sessionmaker

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    engine, session_maker = create_engine_and_sessionmaker(database_url)
    try:
        async with session_maker() as session:
            await seed_demo_data(session)
    finally:
        await engine.dispose()


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


async def _run_benchmarks(base_url: str) -> dict[str, object]:
    limits = httpx.Limits(max_connections=150, max_keepalive_connections=150)
    async with httpx.AsyncClient(
        base_url=base_url,
        limits=limits,
        timeout=httpx.Timeout(30.0),
    ) as client:
        await _wait_for_health(client)

        owner_token = await _login(client, "owner@slotguard.dev", "Owner@1234")
        member_token = await _login(client, "member@slotguard.dev", "Member@1234")
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        member_headers = {"Authorization": f"Bearer {member_token}"}

        # Create isolated benchmark resources (5 resources x 12 hourly slots = 60 distinct slots)
        bench_resource_ids: list[int] = []
        for idx in range(1, 6):
            res = await client.post(
                "/api/v1/resources",
                json={
                    "name": f"Benchmark Court {idx}",
                    "resource_type": "SPORTS_COURT",
                    "description": "Load benchmark resource",
                    "capacity": 4,
                    "hourly_rate_cents": 3000,
                    "open_time": "08:00:00",
                    "close_time": "20:00:00",
                    "slot_duration_minutes": 60,
                },
                headers=owner_headers,
            )
            res.raise_for_status()
            bench_resource_ids.append(int(res.json()["id"]))

        bench_date = datetime.now(UTC).date() + timedelta(days=10)
        race_date = bench_date + timedelta(days=1)

        # Scenario A: 100-request double-booking race burst on one identical slot
        race_payload = {
            "resource_id": bench_resource_ids[0],
            "start_time": f"{race_date.isoformat()}T14:00:00Z",
            "end_time": f"{race_date.isoformat()}T15:00:00Z",
            "notes": "Scenario A contention race",
        }

        async def _post_race() -> tuple[int, dict[str, object], float]:
            t0 = time.perf_counter()
            r = await client.post("/api/v1/bookings", json=race_payload, headers=member_headers)
            dt = (time.perf_counter() - t0) * 1000.0
            return r.status_code, r.json(), dt

        t_a0 = time.perf_counter()
        results_a = await asyncio.gather(*(_post_race() for _ in range(100)))
        elapsed_a = time.perf_counter() - t_a0

        statuses_a = [item[0] for item in results_a]
        latencies_a = [item[2] for item in results_a]
        winners_a = [item[1] for item in results_a if item[0] == 201]
        assert len(winners_a) == 1, f"Expected strictly 1 winner in Scenario A, got {statuses_a}"
        assert statuses_a.count(409) == 99, (
            f"Expected strictly 99 conflicts in Scenario A, got {statuses_a}"
        )
        winner_booking_id = int(winners_a[0]["id"])

        stats_a = _summarize(
            "A: 100-Request Double-Booking Race Burst",
            statuses_a,
            latencies_a,
            elapsed_a,
            assertions_passed=True,
            notes="1 x 201 HOLD Created, 99 x 409 Conflict Blocked, 0 double-bookings",
        )

        # Scenario B: 60 concurrent distinct-slot bookings across 5 resources x 12 hourly slots
        distinct_payloads = [
            {
                "resource_id": res_id,
                "start_time": f"{bench_date.isoformat()}T{hour:02d}:00:00Z",
                "end_time": f"{bench_date.isoformat()}T{hour + 1:02d}:00:00Z",
                "notes": "Scenario B distinct slot",
            }
            for res_id in bench_resource_ids
            for hour in range(8, 20)
        ]
        assert len(distinct_payloads) == 60

        async def _post_distinct(body: dict[str, object]) -> tuple[int, float]:
            t0 = time.perf_counter()
            r = await client.post("/api/v1/bookings", json=body, headers=member_headers)
            dt = (time.perf_counter() - t0) * 1000.0
            return r.status_code, dt

        t_b0 = time.perf_counter()
        results_b = await asyncio.gather(*(_post_distinct(p) for p in distinct_payloads))
        elapsed_b = time.perf_counter() - t_b0

        statuses_b = [item[0] for item in results_b]
        latencies_b = [item[1] for item in results_b]
        assert statuses_b.count(201) == 60, f"Expected 60 x 201 in Scenario B, got {statuses_b}"

        stats_b = _summarize(
            "B: 60 Concurrent Distinct-Slot Bookings",
            statuses_b,
            latencies_b,
            elapsed_b,
            assertions_passed=True,
            notes="60 x 201 HOLD Created across 5 resources x 12 slots, 0 conflicts/errors",
        )

        # Scenario C: 30 concurrent identical HMAC webhook event replays
        sign_resp = await client.post(
            "/api/v1/webhooks/demo-sign",
            json={
                "booking_id": winner_booking_id,
                "provider": "payos",
                "event_id": "evt_bench_replay_001",
            },
        )
        sign_resp.raise_for_status()
        signed = sign_resp.json()
        wh_headers = {
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": str(signed["timestamp"]),
            "X-Webhook-Signature": str(signed["signature"]),
        }
        raw_bytes = str(signed["raw_body"]).encode("utf-8")

        async def _post_webhook() -> tuple[int, str, float]:
            t0 = time.perf_counter()
            r = await client.post("/api/v1/webhooks/payment", content=raw_bytes, headers=wh_headers)
            dt = (time.perf_counter() - t0) * 1000.0
            return r.status_code, str(r.json().get("status")), dt

        t_c0 = time.perf_counter()
        results_c = await asyncio.gather(*(_post_webhook() for _ in range(30)))
        elapsed_c = time.perf_counter() - t_c0

        statuses_c = [item[0] for item in results_c]
        ack_states_c = [item[1] for item in results_c]
        latencies_c = [item[2] for item in results_c]
        assert all(code == 200 for code in statuses_c), f"Expected 30 x 200 OK, got {statuses_c}"
        assert ack_states_c.count("processed") == 1, f"Expected 1 processed, got {ack_states_c}"
        assert ack_states_c.count("already_processed") == 29

        booking_check = await client.get(
            f"/api/v1/bookings/{winner_booking_id}",
            headers=member_headers,
        )
        booking_check.raise_for_status()
        assert booking_check.json()["status"] == "CONFIRMED"

        outbox_check = await client.get("/api/v1/outbox/recent?limit=100")
        outbox_check.raise_for_status()
        winner_outbox_rows = [
            row for row in outbox_check.json()["items"] if row["booking_id"] == winner_booking_id
        ]
        assert len(winner_outbox_rows) == 1, (
            f"Expected exact 1 outbox row for winner booking, got {len(winner_outbox_rows)}"
        )

        stats_c = _summarize(
            "C: 30 Concurrent Identical Webhook Replays",
            statuses_c,
            latencies_c,
            elapsed_c,
            assertions_passed=True,
            notes=(
                "30 x 200 OK (1 processed, 29 already_processed), 1 CONFIRMED booking, 1 outbox row"
            ),
        )

        metrics_resp = await client.get("/api/v1/metrics/summary")
        metrics_resp.raise_for_status()

        return {
            "server_url": base_url,
            "scenarios": [asdict(stats_a), asdict(stats_b), asdict(stats_c)],
            "metrics_summary": metrics_resp.json(),
        }


async def run_suite() -> dict[str, object]:
    """Boot disposable SQLite WAL + Uvicorn server on ephemeral port and run all scenarios."""
    with tempfile.TemporaryDirectory(prefix="slotguard_bench_", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "slotguard_bench.db"
        database_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
        await _prepare_database(database_url)

        port = _find_free_port()
        env = os.environ.copy()
        env.update(
            {
                "APP_ENV": "benchmark",
                "DATABASE_URL": database_url,
                "RATE_LIMIT_BACKEND": "memory",
                "RATE_LIMIT_AUTH_REQUESTS": "1000",
                "RATE_LIMIT_BOOKING_REQUESTS": "10000",
                "RATE_LIMIT_WEBHOOK_REQUESTS": "10000",
                "WORKER_ENABLED": "false",
                "JWT_SECRET": "benchmark-secret-key-at-least-32-chars",
                "WEBHOOK_SECRET": "benchmark-webhook-secret-32-chars",
            }
        )

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "slotguard.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            return await _run_benchmarks(f"http://127.0.0.1:{port}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _print_report(report: dict[str, object]) -> None:
    print("\n=== SlotGuard v2.0.0 Load & Concurrency Benchmark Report ===")
    for item in report["scenarios"]:  # type: ignore[index]
        print(
            f"\n[{item['name']}]\n"
            f"  Requests: {item['total_requests']} | Statuses: {item['status_counts']}\n"
            f"  Elapsed:  {item['elapsed_seconds']}s | Throughput: {item['rps']} req/s\n"
            f"  Latency:  p50={item['p50_ms']}ms | p95={item['p95_ms']}ms | "
            f"p99={item['p99_ms']}ms | min={item['min_ms']}ms | max={item['max_ms']}ms\n"
            f"  Verified: {item['assertions_passed']} ({item['notes']})"
        )
    ms = report["metrics_summary"]  # type: ignore[index]
    print(
        "\n[Server /api/v1/metrics/summary]\n"
        f"  requests.total={ms['requests']['total']} "
        f"(2xx={ms['requests']['status_2xx']}, 4xx={ms['requests']['status_4xx']}, "
        f"5xx={ms['requests']['status_5xx']})\n"
        f"  bookings: holds_created={ms['bookings']['holds_created']}, "
        f"conflicts_blocked_409={ms['bookings']['conflicts_blocked_409']}\n"
        f"  webhooks: verified={ms['webhooks']['verified']}, "
        f"processed={ms['webhooks']['processed']}, "
        f"idempotent_replays={ms['webhooks']['idempotent_replays']}, "
        f"rejected={ms['webhooks']['rejected']}\n"
        f"  server latency: p50={ms['latency_ms']['p50']}ms, p95={ms['latency_ms']['p95']}ms\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run SlotGuard v2 load and concurrency benchmarks."
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="Run against an already-running SlotGuard API URL instead of ephemeral SQLite.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")
    args = parser.parse_args()

    if args.base_url:
        report = asyncio.run(_run_benchmarks(args.base_url))
    else:
        report = asyncio.run(run_suite())
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)


if __name__ == "__main__":
    main()
