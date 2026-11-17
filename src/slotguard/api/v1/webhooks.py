"""Payment gateway webhook endpoint secured by raw-body HMAC-SHA256 verification."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.api.deps import get_app_clock, get_app_settings, get_resource_locks, rate_limit
from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.database import get_db_session
from slotguard.core.locks import ResourceLockManager
from slotguard.schemas.webhook import WebhookAckResponse
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
    )
    return await service.process_payment_webhook(
        raw_body=raw_body,
        timestamp_header=x_webhook_timestamp,
        signature_header=x_webhook_signature,
    )
