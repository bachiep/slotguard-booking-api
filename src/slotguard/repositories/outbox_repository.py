"""Async repository for transactional outbox notifications."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.models.webhook import OutboxNotification, OutboxNotificationStatus


class OutboxRepository:
    """Data access helpers for transactional outbox notifications."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, notification: OutboxNotification) -> OutboxNotification:
        self._session.add(notification)
        await self._session.flush()
        return notification

    async def list_pending(self, *, limit: int = 50) -> list[OutboxNotification]:
        stmt = (
            select(OutboxNotification)
            .where(OutboxNotification.status == OutboxNotificationStatus.PENDING.value)
            .order_by(OutboxNotification.created_at.asc(), OutboxNotification.id.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())
