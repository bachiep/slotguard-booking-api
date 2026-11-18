"""Thread- and async-safe in-process telemetry collector for SlotGuard operations."""

import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from slotguard.core.clock import Clock, SystemClock


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Compute nearest-rank percentile on a non-empty sorted sequence."""
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, math.ceil(pct * len(sorted_values)) - 1))
    return round(sorted_values[index], 2)


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """Point-in-time immutable telemetry snapshot."""

    started_at: datetime
    timestamp: datetime
    uptime_seconds: float
    requests_total: int
    status_2xx: int
    status_4xx: int
    status_5xx: int
    rate_limit_hits_429: int
    latency_sample_count: int
    latency_p50_ms: float
    latency_p95_ms: float
    latency_avg_ms: float
    holds_created: int
    conflicts_blocked_409: int
    cancellations: int
    webhooks_verified: int
    webhooks_processed: int
    webhooks_idempotent_replays: int
    webhooks_rejected: int
    webhooks_ignored: int
    expired_holds: int
    outbox_processed: int
    outbox_failed: int


class SlotGuardMetrics:
    """Lock-protected telemetry counters and rolling request latency window."""

    def __init__(self, *, clock: Clock | None = None, latency_window_size: int = 1000) -> None:
        self._clock = clock or SystemClock()
        self._started_at = self._clock.now()
        self._lock = threading.Lock()
        self._latencies_ms: deque[float] = deque(maxlen=max(1, latency_window_size))

        self._requests_total = 0
        self._status_2xx = 0
        self._status_4xx = 0
        self._status_5xx = 0
        self._rate_limit_hits_429 = 0

        self._holds_created = 0
        self._conflicts_blocked_409 = 0
        self._cancellations = 0

        self._webhooks_verified = 0
        self._webhooks_processed = 0
        self._webhooks_idempotent_replays = 0
        self._webhooks_rejected = 0
        self._webhooks_ignored = 0

        self._expired_holds = 0
        self._outbox_processed = 0
        self._outbox_failed = 0

    def record_request(self, status_code: int, duration_ms: float) -> None:
        """Record an HTTP response status class and duration in milliseconds."""
        bounded_ms = max(0.0, float(duration_ms))
        with self._lock:
            self._requests_total += 1
            if 200 <= status_code < 300:
                self._status_2xx += 1
            elif 400 <= status_code < 500:
                self._status_4xx += 1
            elif status_code >= 500:
                self._status_5xx += 1
            self._latencies_ms.append(bounded_ms)

    def record_rate_limit_hit(self) -> None:
        """Increment 429 rate-limit rejection count."""
        with self._lock:
            self._rate_limit_hits_429 += 1

    def record_hold_created(self) -> None:
        """Increment newly created HOLD booking count."""
        with self._lock:
            self._holds_created += 1

    def record_booking_conflict(self) -> None:
        """Increment 409 double-booking conflict block count."""
        with self._lock:
            self._conflicts_blocked_409 += 1

    def record_booking_cancelled(self) -> None:
        """Increment cancelled booking count."""
        with self._lock:
            self._cancellations += 1

    def record_webhook_verified(self) -> None:
        """Increment cryptographically verified webhook count."""
        with self._lock:
            self._webhooks_verified += 1

    def record_webhook_processed(self) -> None:
        """Increment newly processed payment confirmation webhook count."""
        with self._lock:
            self._webhooks_processed += 1

    def record_webhook_replay(self) -> None:
        """Increment idempotently deduplicated webhook replay count."""
        with self._lock:
            self._webhooks_idempotent_replays += 1

    def record_webhook_rejected(self) -> None:
        """Increment rejected webhook count (bad signature, replay window, or payload conflict)."""
        with self._lock:
            self._webhooks_rejected += 1

    def record_webhook_ignored(self) -> None:
        """Increment valid-signature webhook count ignored by booking state rules."""
        with self._lock:
            self._webhooks_ignored += 1

    def record_holds_expired(self, count: int) -> None:
        """Add expired HOLD bookings swept by maintenance."""
        if count <= 0:
            return
        with self._lock:
            self._expired_holds += count

    def record_outbox_processed(self, count: int) -> None:
        """Add outbox notifications successfully dispatched."""
        if count <= 0:
            return
        with self._lock:
            self._outbox_processed += count

    def record_outbox_failed(self) -> None:
        """Increment failed outbox notification delivery attempts."""
        with self._lock:
            self._outbox_failed += 1

    def snapshot(self) -> MetricsSnapshot:
        """Return a consistent telemetry snapshot including p50/p95 latencies."""
        now = self._clock.now()
        with self._lock:
            samples = sorted(self._latencies_ms)
            sample_count = len(samples)
            avg_ms = round(sum(samples) / sample_count, 2) if sample_count else 0.0
            return MetricsSnapshot(
                started_at=self._started_at,
                timestamp=now,
                uptime_seconds=max(0.0, round((now - self._started_at).total_seconds(), 2)),
                requests_total=self._requests_total,
                status_2xx=self._status_2xx,
                status_4xx=self._status_4xx,
                status_5xx=self._status_5xx,
                rate_limit_hits_429=self._rate_limit_hits_429,
                latency_sample_count=sample_count,
                latency_p50_ms=_percentile(samples, 0.50),
                latency_p95_ms=_percentile(samples, 0.95),
                latency_avg_ms=avg_ms,
                holds_created=self._holds_created,
                conflicts_blocked_409=self._conflicts_blocked_409,
                cancellations=self._cancellations,
                webhooks_verified=self._webhooks_verified,
                webhooks_processed=self._webhooks_processed,
                webhooks_idempotent_replays=self._webhooks_idempotent_replays,
                webhooks_rejected=self._webhooks_rejected,
                webhooks_ignored=self._webhooks_ignored,
                expired_holds=self._expired_holds,
                outbox_processed=self._outbox_processed,
                outbox_failed=self._outbox_failed,
            )
