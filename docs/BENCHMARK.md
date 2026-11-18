# SlotGuard v2.0.0 — Load & Concurrency Benchmark Report

Reproducible async load and race-condition benchmark measured against a live `uvicorn` HTTP server backed by an isolated SQLite WAL database on an ephemeral port using `benchmarks/run_load_benchmark.py`.

## Reproduction Command

```bash
.\.venv\Scripts\python benchmarks/run_load_benchmark.py
# Or on Linux / macOS:
.venv/bin/python benchmarks/run_load_benchmark.py
```

The script automatically:
1. Provisions a temporary directory with an isolated SQLite WAL database (`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;`).
2. Applies `alembic upgrade head` and seeds demo users (`scripts/seed_demo.py`).
3. Boots a live `uvicorn slotguard.main:app` subprocess on `127.0.0.1:<ephemeral_port>` with elevated rate-limit thresholds (`10000` req/min) so scenarios measure engine/database throughput rather than hitting `429` throttling.
4. Drives concurrent async `httpx.AsyncClient` bursts and verifies all correctness invariants and `/api/v1/metrics/summary` telemetry.

---

## Measured Results (Windows 11 · Python 3.13.7 · SQLite WAL · Live Uvicorn TCP)

| Scenario | Concurrency & Target | HTTP Breakdown | Throughput | Client p50 / p95 / p99 | Correctness Invariant |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **A: Double-Booking Race Burst** | `100` concurrent `POST /api/v1/bookings` on identical `(resource_id, [14:00, 15:00))` | `1 x 201`, `99 x 409` | **59.1 req/s** (`1.693s`) | `933.53` / `1475.97` / `1595.2 ms` | **0 double-bookings** (`1` `HOLD` committed) |
| **B: Distinct-Slot Throughput** | `60` concurrent `POST /api/v1/bookings` across `5` resources × `12` hourly slots | `60 x 201` | **31.2 req/s** (`1.924s`) | `1008.7` / `1715.53` / `1795.15 ms` | **60 / 60** unique slots reserved cleanly |
| **C: Concurrent Webhook Replays** | `30` concurrent identical signed `POST /api/v1/webhooks/payment` deliveries | `30 x 200` (`1 processed`, `29 already_processed`) | **37.4 req/s** (`0.802s`) | `481.36` / `729.23` / `733.22 ms` | **1** `CONFIRMED` booking, **1** `OutboxNotification` row |

### Server-Side Telemetry Snapshot (`GET /api/v1/metrics/summary`)

| Metric | Measured Value |
| :--- | :--- |
| `requests.total` | `201` (`2xx=102`, `4xx=99`, `5xx=0`) |
| `bookings.holds_created` | `61` (`1` Scenario A + `60` Scenario B) |
| `bookings.conflicts_blocked_409` | `99` |
| `webhooks.verified` / `processed` / `idempotent_replays` / `rejected` | `30` / `1` / `29` / `0` |
| Server Handler Latency (`p50` / `p95` across all 201 requests) | `124.49 ms` / `777.96 ms` |

---

## Engineering Analysis

1. **Why client burst p50 differs from per-request server p50 in Scenario A:**
   `asyncio.gather` dispatches all 100 overlapping requests at `t=0`. `ResourceLockManager` serializes critical sections per `resource_id` so overlapping queries always observe a committed snapshot, queuing contender #50 behind contenders #1–#49 while preserving strict `1 x 201` / `99 x 409` correctness.
2. **SQLite WAL vs. PostgreSQL 16 under Scenario B:**
   Even across 5 distinct `resource_id` locks (`asyncio.Lock`), SQLite enforces single-writer serialization at the database file level. Deploying against PostgreSQL 16 (`docker compose up`) replaces file-level write locking with row-level `SELECT ... FOR UPDATE` on each resource row, enabling true parallel writes across independent resources.
3. **Savepoint-Safe Webhook Idempotency under Scenario C:**
   When 30 identical signed webhooks race past the initial read check simultaneously, `WebhookService` serializes on the target booking's resource lock and inserts `(provider, event_id)` inside a nested savepoint (`begin_nested()`), converting `IntegrityError` cleanly into `200 OK` (`already_processed`) with zero duplicate outbox rows.
