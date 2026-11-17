"""Per-resource asynchronous locking primitives for booking critical sections."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ResourceLockManager:
    """Maintains per-resource asyncio locks to serialize concurrent slot reservations."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    @asynccontextmanager
    async def acquire(self, resource_id: int) -> AsyncIterator[None]:
        """Acquire the exclusive in-process lock for a given resource_id."""
        lock = self._locks.setdefault(resource_id, asyncio.Lock())
        async with lock:
            yield
