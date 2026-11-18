"""Observability summary and transactional outbox inspection endpoints."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard import __version__
from slotguard.api.deps import (
    get_app_clock,
    get_app_settings,
    get_metrics,
    get_resource_locks,
)
from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.database import get_db_session
from slotguard.core.exceptions import ForbiddenError
from slotguard.core.locks import ResourceLockManager
from slotguard.core.metrics import SlotGuardMetrics
from slotguard.core.problem_details import ProblemDetail
from slotguard.models.booking import BookingStatus
from slotguard.models.webhook import OutboxNotificationStatus
from slotguard.repositories.booking_repository import BookingRepository
from slotguard.repositories.outbox_repository import OutboxRepository
from slotguard.schemas.ops import (
    BookingMetricsSummary,
    DatabaseTotalsSummary,
    LatencyMetricsSummary,
    MaintenanceMetricsSummary,
    MaintenanceRunResponse,
    MetricsSummaryResponse,
    OutboxRecentItem,
    OutboxRecentResponse,
    RequestMetricsSummary,
    WebhookMetricsSummary,
)
from slotguard.services.maintenance_service import MaintenanceService

router = APIRouter(tags=["Observability"])

_SAFE_OUTBOX_KEYS = frozenset(
    {
        "provider",
        "event_id",
        "event_type",
        "booking_id",
        "resource_id",
        "start_time",
        "end_time",
        "payment_reference",
        "amount_cents",
        "expired_at",
    }
)


def _sanitize_payload_summary(raw_payload_json: str) -> dict[str, Any]:
    """Parse stored outbox JSON and project only non-sensitive summary fields."""
    try:
        parsed = json.loads(raw_payload_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {key: value for key, value in parsed.items() if key in _SAFE_OUTBOX_KEYS}


@router.get(
    "/metrics/summary",
    response_model=MetricsSummaryResponse,
    summary="Runtime telemetry and inventory metrics summary",
    responses={
        200: {"description": "Current telemetry counters, latency percentiles, and DB totals"}
    },
)
async def get_metrics_summary(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    metrics: Annotated[SlotGuardMetrics, Depends(get_metrics)],
) -> MetricsSummaryResponse:
    """Return runtime request, conflict, webhook, rate-limit, and outbox telemetry."""
    snap = metrics.snapshot()
    booking_counts = await BookingRepository(session).count_by_status()
    outbox_counts = await OutboxRepository(session).count_by_status()

    return MetricsSummaryResponse(
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        uptime_seconds=snap.uptime_seconds,
        timestamp=snap.timestamp,
        requests=RequestMetricsSummary(
            total=snap.requests_total,
            status_2xx=snap.status_2xx,
            status_4xx=snap.status_4xx,
            status_5xx=snap.status_5xx,
            rate_limit_hits_429=snap.rate_limit_hits_429,
        ),
        latency_ms=LatencyMetricsSummary(
            sample_count=snap.latency_sample_count,
            p50=snap.latency_p50_ms,
            p95=snap.latency_p95_ms,
            avg=snap.latency_avg_ms,
        ),
        bookings=BookingMetricsSummary(
            holds_created=snap.holds_created,
            conflicts_blocked_409=snap.conflicts_blocked_409,
            cancellations=snap.cancellations,
        ),
        webhooks=WebhookMetricsSummary(
            verified=snap.webhooks_verified,
            processed=snap.webhooks_processed,
            idempotent_replays=snap.webhooks_idempotent_replays,
            rejected=snap.webhooks_rejected,
            ignored=snap.webhooks_ignored,
        ),
        maintenance=MaintenanceMetricsSummary(
            expired_holds=snap.expired_holds,
            outbox_processed=snap.outbox_processed,
            outbox_failed=snap.outbox_failed,
        ),
        database_totals=DatabaseTotalsSummary(
            bookings_hold=booking_counts.get(BookingStatus.HOLD.value, 0),
            bookings_confirmed=booking_counts.get(BookingStatus.CONFIRMED.value, 0),
            bookings_cancelled=booking_counts.get(BookingStatus.CANCELLED.value, 0),
            bookings_expired=booking_counts.get(BookingStatus.EXPIRED.value, 0),
            outbox_pending=outbox_counts.get(OutboxNotificationStatus.PENDING.value, 0),
            outbox_sent=outbox_counts.get(OutboxNotificationStatus.SENT.value, 0),
            outbox_failed=outbox_counts.get(OutboxNotificationStatus.FAILED.value, 0),
        ),
    )


@router.get(
    "/outbox/recent",
    response_model=OutboxRecentResponse,
    summary="Inspect recent transactional outbox notifications",
    responses={
        200: {"description": "Newest transactional outbox notification rows"},
        422: {"model": ProblemDetail, "description": "Invalid query parameter"},
    },
)
async def get_recent_outbox(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = Query(default=20, ge=1, le=100),
) -> OutboxRecentResponse:
    """Return newest OutboxNotification rows with safe sanitized payload summaries."""
    items, total = await OutboxRepository(session).list_recent(limit=limit)
    return OutboxRecentResponse(
        items=[
            OutboxRecentItem(
                id=row.id,
                booking_id=row.booking_id,
                event_type=row.notification_type,
                status=row.status,
                attempts=row.attempts,
                created_at=row.created_at,
                sent_at=row.sent_at,
                payload_summary=_sanitize_payload_summary(row.payload_json),
            )
            for row in items
        ],
        total=total,
    )


@router.post(
    "/ops/run-maintenance",
    response_model=MaintenanceRunResponse,
    summary="Trigger an on-demand hold expiry and outbox delivery sweep (non-production only)",
    responses={
        200: {"description": "Maintenance sweep completed successfully"},
        403: {"model": ProblemDetail, "description": "Disabled in production environment"},
    },
)
async def run_maintenance(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
    lock_manager: Annotated[ResourceLockManager, Depends(get_resource_locks)],
    metrics: Annotated[SlotGuardMetrics, Depends(get_metrics)],
) -> MaintenanceRunResponse:
    """Run one maintenance cycle outside production environments."""
    if settings.environment.strip().lower() == "production":
        raise ForbiddenError("Maintenance sweep endpoint is disabled in production environments.")
    service = MaintenanceService(
        session=session,
        clock=clock,
        lock_manager=lock_manager,
        metrics=metrics,
    )
    result = await service.run_cycle()
    return MaintenanceRunResponse(**result)
