"""Idempotent payment webhook handler with raw-body HMAC verification and outbox enqueue."""

import hashlib
import json

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.exceptions import (
    AppError,
    BusinessRuleError,
    ConflictError,
    NotFoundError,
)
from slotguard.core.locks import ResourceLockManager
from slotguard.core.webhook_security import verify_webhook_headers
from slotguard.models.booking import BookingStatus
from slotguard.models.webhook import (
    NotificationType,
    OutboxNotification,
    OutboxNotificationStatus,
    WebhookEvent,
    WebhookEventStatus,
)
from slotguard.repositories.booking_repository import BookingRepository
from slotguard.repositories.outbox_repository import OutboxRepository
from slotguard.repositories.user_repository import UserRepository
from slotguard.repositories.webhook_repository import WebhookRepository
from slotguard.schemas.webhook import PaymentWebhookPayload, WebhookAckResponse


class WebhookService:
    """Verify HMAC-SHA256 callbacks, deduplicate events, and confirm HOLD bookings atomically."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        clock: Clock,
        lock_manager: ResourceLockManager,
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._lock_manager = lock_manager
        self._webhooks = WebhookRepository(session)
        self._bookings = BookingRepository(session)
        self._users = UserRepository(session)
        self._outbox = OutboxRepository(session)

    @staticmethod
    def _parse_payload(raw_body: bytes) -> PaymentWebhookPayload:
        try:
            return PaymentWebhookPayload.model_validate_json(raw_body)
        except ValidationError as exc:
            sanitized = [
                {
                    "loc": list(err.get("loc", ())),
                    "msg": str(err.get("msg", "Invalid value")),
                    "type": str(err.get("type", "value_error")),
                }
                for err in exc.errors()
            ]
            raise AppError(
                "Webhook payload validation failed.",
                status_code=422,
                title="Validation Error",
                error_type="https://slotguard.dev/errors/validation-error",
                errors=sanitized,
            ) from exc

    async def process_payment_webhook(
        self,
        *,
        raw_body: bytes,
        timestamp_header: str | None,
        signature_header: str | None,
    ) -> WebhookAckResponse:
        """Verify signature/timestamp and atomically process the payment webhook event."""
        verify_webhook_headers(
            timestamp_header=timestamp_header,
            signature_header=signature_header,
            raw_body=raw_body,
            settings=self._settings,
            clock=self._clock,
        )
        payload = self._parse_payload(raw_body)
        payload_hash = hashlib.sha256(raw_body).hexdigest()

        existing = await self._webhooks.get_by_provider_event_id(payload.provider, payload.event_id)
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise ConflictError(
                    "Webhook event_id was previously processed with a differing payload."
                )
            booking = await self._bookings.get_by_id(payload.booking_id)
            return WebhookAckResponse(
                status="already_processed",
                event_id=payload.event_id,
                booking_id=payload.booking_id,
                booking_status=booking.status if booking is not None else None,
            )

        target_booking = await self._bookings.get_by_id(payload.booking_id)
        if target_booking is None:
            raise NotFoundError(f"Booking {payload.booking_id} was not found.")

        async with self._lock_manager.acquire(target_booking.resource_id):
            await self._session.rollback()

            booking = await self._bookings.get_by_id(payload.booking_id)
            if booking is None:
                raise NotFoundError(f"Booking {payload.booking_id} was not found.")

            now = self._clock.now()
            event_record = WebhookEvent(
                provider=payload.provider,
                event_id=payload.event_id,
                event_type=payload.event_type,
                booking_id=booking.id,
                payload_hash=payload_hash,
                status=WebhookEventStatus.PROCESSED.value,
                received_at=now,
            )
            try:
                async with self._session.begin_nested():
                    await self._webhooks.create(event_record)
            except IntegrityError as exc:
                existing_after_race = await self._webhooks.get_by_provider_event_id(
                    payload.provider, payload.event_id
                )
                if existing_after_race is None:
                    raise
                if existing_after_race.payload_hash != payload_hash:
                    raise ConflictError(
                        "Webhook event_id was previously processed with a differing payload."
                    ) from exc
                return WebhookAckResponse(
                    status="already_processed",
                    event_id=payload.event_id,
                    booking_id=booking.id,
                    booking_status=booking.status,
                )

            if (
                payload.event_type != "payment.succeeded"
                or booking.status != BookingStatus.HOLD.value
            ):
                event_record.status = WebhookEventStatus.IGNORED.value
                await self._session.commit()
                return WebhookAckResponse(
                    status="ignored",
                    event_id=payload.event_id,
                    booking_id=booking.id,
                    booking_status=booking.status,
                )

            if payload.amount_cents != booking.total_price_cents:
                await self._session.rollback()
                raise BusinessRuleError("Webhook payment amount does not match booking total.")

            user = await self._users.get_by_id(booking.user_id)
            if user is None:
                await self._session.rollback()
                raise NotFoundError("Booking owner account was not found.")

            booking.status = BookingStatus.CONFIRMED.value
            booking.confirmed_at = now
            booking.payment_reference = payload.payment_reference
            booking.updated_at = now

            notification = OutboxNotification(
                booking_id=booking.id,
                recipient_email=user.email,
                notification_type=NotificationType.BOOKING_CONFIRMED.value,
                payload_json=json.dumps(
                    {
                        "booking_id": booking.id,
                        "resource_id": booking.resource_id,
                        "start_time": booking.start_time.isoformat(),
                        "end_time": booking.end_time.isoformat(),
                        "payment_reference": payload.payment_reference,
                        "amount_cents": booking.total_price_cents,
                    }
                ),
                status=OutboxNotificationStatus.PENDING.value,
                attempts=0,
                created_at=now,
            )
            await self._outbox.enqueue(notification)
            await self._session.commit()

            return WebhookAckResponse(
                status="processed",
                event_id=payload.event_id,
                booking_id=booking.id,
                booking_status=booking.status,
            )
