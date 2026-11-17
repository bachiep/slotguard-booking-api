"""Async repository for Resource persistence and filtered pagination."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.models.resource import Resource


class ResourceRepository:
    """Data access helpers for bookable resources."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, resource_id: int) -> Resource | None:
        result = await self._session.execute(select(Resource).where(Resource.id == resource_id))
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Resource | None:
        result = await self._session.execute(select(Resource).where(Resource.name == name.strip()))
        return result.scalar_one_or_none()

    async def create(self, resource: Resource) -> Resource:
        self._session.add(resource)
        await self._session.flush()
        return resource

    async def list_paginated(
        self,
        *,
        resource_type: str | None = None,
        active_only: bool = True,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Resource], int]:
        filters = []
        if active_only:
            filters.append(Resource.is_active.is_(True))
        if resource_type is not None:
            filters.append(Resource.resource_type == resource_type)

        count_stmt = select(func.count(Resource.id)).where(*filters)
        total = int((await self._session.execute(count_stmt)).scalar_one())

        items_stmt = (
            select(Resource).where(*filters).order_by(Resource.id.asc()).limit(limit).offset(offset)
        )
        items = list((await self._session.execute(items_stmt)).scalars().all())
        return items, total
