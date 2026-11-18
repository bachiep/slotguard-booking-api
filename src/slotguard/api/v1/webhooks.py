"""Payment gateway webhook endpoints with HMAC-SHA256 verification and demo signing."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.api.deps import (
    get_app_clock,
    get_app_settings,
    get_metrics,
    get_resource_locks,
    rate_limit,
)
from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.database import get_db_session
from slotguard.core.locks import ResourceLockManager
from slotguard.core.metrics import SlotGuardMetrics
from slotguard.core.problem_details import ProblemDetail
from slotguard.schemas.webhook import (
    WebhookAckResponse,
    WebhookDemoSignRequest,
    WebhookDemoSignResponse,
)
from slotguard.services.webhook_service import WebhookService

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.post(
    "/payment",
    response_model=WebhookAckResponse,
    status_code=status.HTTP_200_OK,
    summary="Process payment confirmation webhook (HMAC-SHA256 + idempotent)",
    dependencies=[
        Depends(
            rate_limit("webhooks_payment", limit_getter=lambda s: s.rate_limit_webhook_requests)
        )
    ],
    responses={
        401: {"model": ProblemDetail, "description": "Invalid signature or stale timestamp"},
        404: {"model": ProblemDetail, "description": "Target booking not found"},
        409: {"model": ProblemDetail, "description": "Duplicate event_id with differing payload"},
        422: {"model": ProblemDetail, "description": "Payload validation or amount mismatch"},
        429: {"model": ProblemDetail, "description": "Webhook rate limit exceeded"},
    },
    description=(
        "Verifies X-Webhook-Timestamp (300s replay guard) and X-Webhook-Signature "
        "(sha256=HMAC_SHA256(secret, '<timestamp>.<raw_body>')) over raw request bytes, "
        "deduplicates on (provider, event_id), confirms HOLD bookings, and writes an outbox row."
    ),
)
async def handle_payment_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
    lock_manager: Annotated[ResourceLockManager, Depends(get_resource_locks)],
    metrics: Annotated[SlotGuardMetrics, Depends(get_metrics)],
    x_webhook_timestamp: Annotated[str | None, Header()] = None,
    x_webhook_signature: Annotated[str | None, Header()] = None,
) -> WebhookAckResponse:
    """Process an incoming payment provider callback safely and idempotently."""
    raw_body = await request.body()
    service = WebhookService(
        session=session,
        settings=settings,
        clock=clock,
        lock_manager=lock_manager,
        metrics=metrics,
    )
    return await service.process_payment_webhook(
        raw_body=raw_body,
        timestamp_header=x_webhook_timestamp,
        signature_header=x_webhook_signature,
    )


@router.post(
    "/demo-sign",
    response_model=WebhookDemoSignResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign a demo payment webhook payload (non-production only)",
    responses={
        403: {"model": ProblemDetail, "description": "Disabled in production environment"},
        404: {"model": ProblemDetail, "description": "Target booking not found"},
        422: {"model": ProblemDetail, "description": "Validation error"},
    },
    description=(
        "Signs exact serialized JSON bytes with the server webhook secret so non-production "
        "consoles can test HMAC verification without embedding secrets in client code."
    ),
)
async def demo_sign_webhook(
    payload: WebhookDemoSignRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_app_clock)],
    lock_manager: Annotated[ResourceLockManager, Depends(get_resource_locks)],
    metrics: Annotated[SlotGuardMetrics, Depends(get_metrics)],
) -> WebhookDemoSignResponse:
    """Generate signed HMAC headers and exact raw body bytes outside production."""
    service = WebhookService(
        session=session,
        settings=settings,
        clock=clock,
        lock_manager=lock_manager,
        metrics=metrics,
    )
    return await service.sign_demo_payload(payload)
