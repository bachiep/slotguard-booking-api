"""Async repository for WebhookEvent idempotency records."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.models.webhook import WebhookEvent


class WebhookRepository:
    """Data access helpers for deduplicating payment gateway webhook events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_provider_event_id(
        self,
        provider: str,
        event_id: str,
    ) -> WebhookEvent | None:
        result = await self._session.execute(
            select(WebhookEvent).where(
                WebhookEvent.provider == provider,
                WebhookEvent.event_id == event_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, event: WebhookEvent) -> WebhookEvent:
        self._session.add(event)
        await self._session.flush()
        return event
