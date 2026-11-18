"""Maintenance service expiring stale HOLD bookings and dispatching outbox notifications."""

import json
import logging
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import Clock
from slotguard.core.locks import ResourceLockManager
from slotguard.core.metrics import SlotGuardMetrics
from slotguard.models.booking import Booking, BookingStatus
from slotguard.models.webhook import (
    NotificationType,
    OutboxNotification,
    OutboxNotificationStatus,
)
from slotguard.repositories.outbox_repository import OutboxRepository
from slotguard.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)


class NotificationSender(Protocol):
    """Outbound notification delivery adapter."""

    async def send(self, notification: OutboxNotification) -> None:
        """Deliver a single outbox notification."""
        ...


class LoggingNotificationSender:
    """Default local/Docker outbox dispatcher logging structured delivery events."""

    async def send(self, notification: OutboxNotification) -> None:
        """Log notification payload as dispatched."""
        logger.info(
            "Dispatched outbox notification id=%s type=%s recipient=%s booking_id=%s",
            notification.id,
            notification.notification_type,
            notification.recipient_email,
            notification.booking_id,
        )


class MaintenanceService:
    """Sweeps expired HOLD bookings and delivers pending transactional outbox notifications."""

    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        lock_manager: ResourceLockManager,
        sender: NotificationSender | None = None,
        metrics: SlotGuardMetrics | None = None,
    ) -> None:
        self._session = session
        self._clock = clock
        self._lock_manager = lock_manager
        self._sender = sender or LoggingNotificationSender()
        self._metrics = metrics
        self._users = UserRepository(session)
        self._outbox = OutboxRepository(session)

    async def expire_stale_holds(self, *, batch_size: int = 50) -> int:
        """Transition expired HOLD rows to EXPIRED in bounded batches and enqueue notices."""
        now = self._clock.now()
        stmt = (
            select(
                Booking.id,
                Booking.resource_id,
                Booking.user_id,
                Booking.start_time,
                Booking.end_time,
            )
            .where(
                Booking.status == BookingStatus.HOLD.value,
                Booking.hold_expires_at <= now,
            )
            .order_by(Booking.hold_expires_at.asc(), Booking.id.asc())
            .limit(batch_size)
        )
        candidates = list((await self._session.execute(stmt)).all())
        expired_count = 0

        for booking_id, resource_id, user_id, start_time, end_time in candidates:
            async with self._lock_manager.acquire(resource_id):
                await self._session.rollback()
                current_now = self._clock.now()
                update_stmt = (
                    update(Booking)
                    .where(
                        Booking.id == booking_id,
                        Booking.status == BookingStatus.HOLD.value,
                        Booking.hold_expires_at <= current_now,
                    )
                    .values(
                        status=BookingStatus.EXPIRED.value,
                        updated_at=current_now,
                    )
                )
                result = await self._session.execute(update_stmt)
                if int(result.rowcount or 0) != 1:
                    await self._session.rollback()
                    continue

                user = await self._users.get_by_id(user_id)
                if user is not None:
                    notice = OutboxNotification(
                        booking_id=booking_id,
                        recipient_email=user.email,
                        notification_type=NotificationType.BOOKING_EXPIRED.value,
                        payload_json=json.dumps(
                            {
                                "booking_id": booking_id,
                                "resource_id": resource_id,
                                "start_time": start_time.isoformat(),
                                "end_time": end_time.isoformat(),
                                "expired_at": current_now.isoformat(),
                            }
                        ),
                        status=OutboxNotificationStatus.PENDING.value,
                        attempts=0,
                        created_at=current_now,
                    )
                    await self._outbox.enqueue(notice)

                await self._session.commit()
                expired_count += 1

        if self._metrics is not None and expired_count > 0:
            self._metrics.record_holds_expired(expired_count)
        return expired_count

    async def process_pending_outbox_with_stats(self, *, batch_size: int = 50) -> tuple[int, int]:
        """Dispatch PENDING outbox notifications, returning (sent_count, failed_count)."""
        pending = await self._outbox.list_pending(limit=batch_size)
        sent_count = 0
        failed_count = 0

        for item in pending:
            try:
                await self._sender.send(item)
            except Exception:
                logger.exception("Failed delivering outbox notification id=%s", item.id)
                item.attempts += 1
                if item.attempts >= 3:
                    item.status = OutboxNotificationStatus.FAILED.value
                await self._session.commit()
                if self._metrics is not None:
                    self._metrics.record_outbox_failed()
                failed_count += 1
                continue

            item.attempts += 1
            item.status = OutboxNotificationStatus.SENT.value
            item.sent_at = self._clock.now()
            await self._session.commit()
            sent_count += 1

        if self._metrics is not None and sent_count > 0:
            self._metrics.record_outbox_processed(sent_count)
        return sent_count, failed_count

    async def process_pending_outbox(self, *, batch_size: int = 50) -> int:
        """Dispatch PENDING outbox notifications and record SENT or FAILED state."""
        sent_count, _ = await self.process_pending_outbox_with_stats(batch_size=batch_size)
        return sent_count

    async def run_cycle(self, *, batch_size: int = 50) -> dict[str, int]:
        """Run one full maintenance cycle: expire overdue holds and dispatch pending outbox."""
        expired = await self.expire_stale_holds(batch_size=batch_size)
        sent, failed = await self.process_pending_outbox_with_stats(batch_size=batch_size)
        return {
            "expired_holds": expired,
            "outbox_sent": sent,
            "outbox_failed": failed,
        }
