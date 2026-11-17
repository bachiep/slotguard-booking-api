"""Background asyncio worker executing hold-expiry sweeps and outbox delivery."""

import asyncio
import contextlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from slotguard.core.clock import Clock
from slotguard.core.locks import ResourceLockManager
from slotguard.services.maintenance_service import MaintenanceService

logger = logging.getLogger(__name__)


class MaintenanceWorker:
    """Periodic background loop for HOLD expiration and outbox notification dispatch."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        clock: Clock,
        lock_manager: ResourceLockManager,
        poll_interval_seconds: int = 30,
    ) -> None:
        self._session_maker = session_maker
        self._clock = clock
        self._lock_manager = lock_manager
        self._poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def run_once(self) -> tuple[int, int]:
        """Run one expiry and outbox sweep cycle."""
        async with self._session_maker() as session:
            service = MaintenanceService(
                session=session,
                clock=self._clock,
                lock_manager=self._lock_manager,
            )
            expired = await service.expire_stale_holds()
            sent = await service.process_pending_outbox()
            return expired, sent

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("Unexpected error inside MaintenanceWorker cycle")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                continue

    def start(self) -> None:
        """Start background worker task."""
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop(), name="slotguard-maintenance-worker")

    async def stop(self) -> None:
        """Signal loop exit and await graceful task shutdown."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
