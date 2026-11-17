"""Injectable clock abstraction for deterministic UTC time handling."""

from datetime import UTC, datetime, timedelta
from typing import Protocol


def ensure_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Clock(Protocol):
    """Protocol for obtaining the current UTC timestamp."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Production clock backed by the system UTC clock."""

    def now(self) -> datetime:
        """Return current UTC datetime."""
        return datetime.now(UTC)


class MutableClock:
    """Controllable clock for deterministic unit and integration tests."""

    def __init__(self, initial: datetime | None = None) -> None:
        self._current = ensure_utc(initial) if initial is not None else datetime.now(UTC)

    def now(self) -> datetime:
        """Return the simulated UTC datetime."""
        return self._current

    def advance(
        self,
        delta: timedelta | None = None,
        *,
        seconds: float = 0,
        minutes: float = 0,
    ) -> datetime:
        """Advance the simulated clock forward and return the new timestamp."""
        step = delta if delta is not None else timedelta(seconds=seconds, minutes=minutes)
        self._current = self._current + step
        return self._current

    def set_time(self, moment: datetime) -> None:
        """Set the simulated clock to a specific UTC datetime."""
        self._current = ensure_utc(moment)
