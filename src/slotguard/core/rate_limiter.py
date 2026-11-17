"""Sliding-window rate limiter interface with In-Memory and Redis Lua backends."""

import asyncio
import math
from collections import deque
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

import redis.asyncio as aioredis

from slotguard.core.clock import Clock
from slotguard.core.config import Settings

_REDIS_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
local cutoff = now - window

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)

if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = 1
    if #oldest >= 2 then
        retry_after = math.max(1, math.ceil((tonumber(oldest[2]) + window) - now))
    end
    return {0, 0, retry_after}
end

redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, math.ceil(window * 1000))
local remaining = math.max(0, limit - (count + 1))
return {1, remaining, 0}
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Decision and response headers returned by a rate-limit check."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class RateLimiter(Protocol):
    """Protocol for sliding-window request rate limiters."""

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Evaluate and record one request against the sliding window."""
        ...

    async def close(self) -> None:
        """Release underlying connections or resources."""
        ...


class InMemorySlidingWindowLimiter:
    """Per-process sliding-window log rate limiter driven by an injectable Clock."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._windows: dict[str, deque[float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Prune expired timestamps and enforce the sliding window quota."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = self._clock.now().timestamp()
            cutoff = now - window_seconds
            entries = self._windows.setdefault(key, deque())
            while entries and entries[0] <= cutoff:
                entries.popleft()

            if len(entries) >= limit:
                oldest = entries[0]
                retry_after = max(1, math.ceil((oldest + window_seconds) - now))
                return RateLimitResult(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    retry_after_seconds=retry_after,
                )

            entries.append(now)
            remaining = max(0, limit - len(entries))
            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=remaining,
                retry_after_seconds=0,
            )

    async def close(self) -> None:
        """Clear in-memory sliding-window state."""
        self._windows.clear()
        self._locks.clear()


class RedisSlidingWindowLimiter:
    """Distributed atomic sliding-window rate limiter backed by Redis Sorted Sets + Lua."""

    def __init__(self, redis_url: str, clock: Clock) -> None:
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._clock = clock

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Execute atomic Lua ZSET sliding-window check in Redis."""
        now = self._clock.now().timestamp()
        member = f"{now}:{uuid4()}"
        raw_result = await self._redis.eval(
            _REDIS_SLIDING_WINDOW_LUA,
            1,
            f"ratelimit:{key}",
            str(now),
            str(window_seconds),
            str(limit),
            member,
        )
        allowed = int(raw_result[0]) == 1
        remaining = int(raw_result[1])
        retry_after = int(raw_result[2])
        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            retry_after_seconds=retry_after,
        )

    async def close(self) -> None:
        """Close the async Redis client pool."""
        await self._redis.aclose()


def build_rate_limiter(settings: Settings, clock: Clock) -> RateLimiter:
    """Select rate limiter backend based on application settings."""
    if settings.rate_limit_backend == "redis":
        return RedisSlidingWindowLimiter(settings.redis_url, clock)
    return InMemorySlidingWindowLimiter(clock)
