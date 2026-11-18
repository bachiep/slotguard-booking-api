"""Async repository for transactional outbox notifications."""

from sqlalchemy import func, select
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

    async def list_recent(self, *, limit: int = 20) -> tuple[list[OutboxNotification], int]:
        """Return newest outbox notification rows alongside total count."""
        total = int(
            (await self._session.execute(select(func.count(OutboxNotification.id)))).scalar_one()
        )
        stmt = (
            select(OutboxNotification)
            .order_by(OutboxNotification.created_at.desc(), OutboxNotification.id.desc())
            .limit(limit)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return items, total

    async def count_by_status(self) -> dict[str, int]:
        """Return outbox notification counts grouped by status."""
        stmt = select(OutboxNotification.status, func.count(OutboxNotification.id)).group_by(
            OutboxNotification.status
        )
        rows = (await self._session.execute(stmt)).all()
        return {str(status_value): int(count) for status_value, count in rows}
